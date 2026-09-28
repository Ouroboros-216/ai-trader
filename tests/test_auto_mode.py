import json

import pytest

from aitrader.provider import CHAT
from aitrader.bridge import atomic_write
from aitrader.multi import AccountRouter
from aitrader.storage import Store


def ready_bars(agent, snapshot, trade_ready=True, h4_bars=True):
    rows = [{"time": n, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.02, "volume": 100}
            for n in range(30)]
    symbols = {name: market | {"ready": trade_ready,
                               "bars": {"M15": rows, "H1": rows, "H4": rows if h4_bars else []}}
               for name, market in snapshot["symbols"].items()}
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot | {"symbols": symbols}))


def test_auto_mode_creates_reviewable_policy_from_completed_market_bars(agent, snapshot, policy):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    preview = agent.handle("自動模式")
    assert "策略草案" in preview and "確認" in preview
    assert agent.provider.calls[-1][0] == "strategy"
    request = agent.provider.calls[-1][1]
    assert request["auto_mode"] is True
    assert request["available_symbols"] == ["XAUUSD", "EURUSD"]
    assert len(request["market_context"]["XAUUSD"]["bars"]["H1"]) == 20
    assert request["bar_fields"] == ["time", "open", "high", "low", "close", "volume"]
    assert request["market_context"]["XAUUSD"]["bars"]["H1"][0] == [10, 1.0, 1.1, 0.9, 1.02, 100]
    assert agent.store.get("paused", True) is True
    agent.confirm(agent.store.get("pending"))
    assert agent.store.get("paused") is True


def test_strategy_preview_is_readable_and_followup_discusses_pending_card(agent, snapshot, policy):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    preview = agent.handle("自動模式")
    pending = agent.store.get("pending")
    assert "進場條件：\n" in preview and "何時失效：\n" in preview
    assert "只做空" in preview and "XAUUSD" in preview and "0.5%" in preview
    assert '"entry":' not in preview and "確認 " + pending in preview
    agent.provider.response = {"answer": "這裡的 BOS 指已完成 K 棒收盤突破結構位。"}
    answer = agent.handle("這張草案的 BOS 是什麼？")
    assert "已完成 K 棒" in answer and "尚未套用" in answer and pending in answer
    assert agent.provider.calls[-1][0] == "chat"
    assert agent.provider.calls[-1][1]["pending_policy"]["title"] == policy["title"]
    assert agent.store.get("pending") == pending


def test_strategy_question_is_discussion_not_a_change(agent, policy):
    agent.provider.response = {"answer": "可以先比較剝頭皮的成本與 1% 單筆風險；目前策略仍是 0.5%。固定手數也可討論。"}
    reply = agent.handle("我在想高頻剝頭皮每筆 1%，你覺得呢？")
    assert "可以先比較" in reply
    assert agent.provider.calls[-1][0] == "chat"
    assert agent.store.get("policy") == policy
    assert agent.store.db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert "不能說「手數不能設定」" in CHAT and "M15 說成 H15" in CHAT


def test_chat_decodes_literal_line_breaks_only_in_answer(agent):
    agent.provider.response = {"answer": "第一段\\n\\n第二段"}
    reply = agent.handle("先討論，不要改策略")
    assert reply == "第一段\n\n第二段"
    saved = agent.conversation_history()[-1]
    assert saved["assistant"] == reply


def test_chat_remembers_discussion_and_explicit_request_creates_only_draft(agent, policy):
    agent.provider.response = {"answer": "可考慮 XAUUSD 剝頭皮，但先定義成本與失效。"}
    agent.handle("比較 XAUUSD 剝頭皮和原本結構策略")
    agent.provider.response = {"policy": policy | {"title": "XAUUSD 剝頭皮", "risk_pct": 1.0}, "questions": []}
    preview = agent.handle("整理成草案 XAUUSD 剝頭皮，每筆 1%")
    assert "策略草案" in preview and "確認 " in preview
    assert agent.provider.calls[-1][0] == "strategy"
    request = agent.provider.calls[-1][1]
    assert request["discussion_history"][0]["user"] == "比較 XAUUSD 剝頭皮和原本結構策略"
    assert agent.store.get("policy") == policy
    assert agent.store.get("paused", True) is True


def test_colon_revision_works_on_confirmed_policy_without_pending_card(agent, policy):
    agent.provider.response = {"policy": policy | {"risk_pct": 1.0}, "questions": []}
    preview = agent.handle("修改：將單筆風險改為 1%")
    assert "單筆 1.0%" in preview
    assert agent.provider.calls[-1][0] == "strategy"
    assert agent.store.get("policy") == policy
    assert agent.store.get("paused", True) is True


def test_modify_pending_card_creates_new_confirmation_without_applying(agent, snapshot, policy):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    agent.handle("自動模式")
    old = agent.store.get("pending")
    agent.provider.response = {"policy": policy | {"title": "新版做空策略"}, "questions": []}
    preview = agent.handle("修改 名稱改成新版做空策略")
    new = agent.store.get("pending")
    assert old != new and "新版做空策略" in preview and "確認 " + new in preview
    assert agent.provider.calls[-1][1]["pending_policy"]["title"] == policy["title"]
    assert agent.store.db.execute("SELECT status FROM proposals WHERE id=?", (old,)).fetchone()[0] == "superseded"
    assert agent.store.get("paused", True) is True


def test_account_router_keeps_confirm_button_after_draft_discussion(agent, snapshot, policy, tmp_path):
    ready_bars(agent, snapshot)
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    agent.provider.response = {"policy": policy, "questions": []}
    router.handle("自動模式")
    pending = agent.store.get("pending")
    agent.provider.response = {"answer": "草案仍待確認。"}
    reply = router.handle("這張草案何時進場？")
    assert pending in reply and router.store.get("pending") == pending
    rows = router.reply_markup("這張草案何時進場？", reply, pending)["inline_keyboard"]
    assert rows[0][0]["text"] == "確認此提案"


def test_auto_mode_requires_usable_completed_bars(agent, policy):
    agent.provider.response = {"policy": policy, "questions": []}
    with pytest.raises(ValueError, match="EA 尚未提供商品 K 棒"):
        agent.handle("自動模式")
    assert not agent.provider.calls


def test_auto_mode_can_draft_while_market_closed_or_commission_unknown(agent, snapshot, policy):
    ready_bars(agent, snapshot, trade_ready=False, h4_bars=False)
    agent.provider.response = {"policy": policy, "questions": []}
    preview = agent.handle("自動模式")
    assert "策略草案" in preview
    assert agent.provider.calls[-1][1]["market_context"]["XAUUSD"]["ready"] is False
    assert agent.provider.calls[-1][1]["market_context"]["XAUUSD"]["bars"]["H4"] == []
    agent.confirm(agent.store.get("pending"))
    assert agent.store.get("paused") is True
    with pytest.raises(ValueError, match="行情尚未就緒"):
        agent.confirm(agent.propose("resume", {}))


def test_rejected_resume_explains_market_and_why_is_readable_without_ai(agent, snapshot):
    ready_bars(agent, snapshot, trade_ready=False)
    agent.handle("啟動")
    identifier = agent.store.get("pending")
    with pytest.raises(ValueError, match="XAUUSD 未就緒.*佣金未知.*M5 0 根.*再傳『啟動』"):
        agent.handle("確認 " + identifier)
    explanation = agent.handle("原因")
    assert "目前行情阻止啟動" in explanation and "XAUUSD 未就緒" in explanation
    assert "確認未套用" in explanation and "confirmed" not in explanation
    assert not agent.provider.calls


def test_invalid_auto_strategy_never_creates_proposal(agent, snapshot):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": ["buy everything"], "questions": []}
    with pytest.raises(ValueError, match="格式不正確"):
        agent.handle("自動模式")
    assert agent.store.get("pending", "") == ""


def test_risk_only_message_changes_confirmed_strategy_without_api_call(agent):
    preview = agent.handle("單筆1%")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (agent.store.get("pending"),)).fetchone()
    assert json.loads(row[0])["risk_pct"] == 1.0
    assert "單筆 1.0%" in preview and "確認 " in preview
    assert not agent.provider.calls
    agent.handle("確認 " + agent.store.get("pending"))
    assert agent.policy().risk_pct == 1.0 and agent.store.get("paused", True) is True


def test_risk_fields_can_be_set_separately_and_total_must_cover_per_trade(agent):
    for message, field, value in (("總風險3%", "total_risk_pct", 3.0),
                                  ("單筆2%", "risk_pct", 2.0),
                                  ("日損4%", "daily_loss_pct", 4.0),
                                  ("回撤10%", "drawdown_pct", 10.0)):
        agent.handle(message)
        row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (agent.store.get("pending"),)).fetchone()
        assert json.loads(row[0])[field] == value
        agent.handle("確認 " + agent.store.get("pending"))
    with pytest.raises(ValueError, match="不可高於總持倉風險"):
        agent.handle("單筆5%")
    assert not agent.provider.calls


def test_risk_change_on_pending_card_replaces_it_without_second_api_call(agent, snapshot, policy):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    agent.handle("自動模式")
    first = agent.store.get("pending")
    preview = agent.handle("單筆1%")
    second = agent.store.get("pending")
    assert second != first and "單筆 1.0%" in preview
    assert agent.store.db.execute("SELECT status FROM proposals WHERE id=?", (first,)).fetchone()[0] == "superseded"
    assert len(agent.provider.calls) == 1


def test_cash_and_fixed_lots_are_reviewable_local_policy_changes(agent):
    cash_preview = agent.handle("每筆虧10美元")
    first = agent.store.get("pending")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (first,)).fetchone()
    cash = json.loads(row[0])
    assert cash["risk_mode"] == "cash" and cash["risk_amount"] == 10
    assert "每筆停損最多 10.0 帳戶幣別" in cash_preview
    lot_preview = agent.handle("固定0.1手")
    second = agent.store.get("pending")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (second,)).fetchone()
    lots = json.loads(row[0])
    assert lots["risk_mode"] == "fixed_lots" and lots["fixed_lots"] == 0.1
    assert lots["risk_amount"] == 0 and "每筆固定 0.1 手" in lot_preview
    assert agent.store.db.execute("SELECT status FROM proposals WHERE id=?", (first,)).fetchone()[0] == "superseded"
    assert not agent.provider.calls


def test_cash_amount_requires_matching_account_currency(agent, snapshot):
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot | {"currency": "EUR"}))
    with pytest.raises(ValueError, match="不能把美元金額直接當成帳戶幣別"):
        agent.handle("每筆虧10美元")
    preview = agent.handle("每筆虧10")
    assert "每筆停損最多 10.0 帳戶幣別" in preview


def test_risk_revision_only_changes_sizing_and_bridge_serializes_decimal(agent):
    preview = agent.handle("固定0.00000001手")
    assert "固定 1e-08 手" in preview
    assert not agent.provider.calls
    agent.handle("確認 " + agent.store.get("pending"))
    agent.publish()
    assert (agent.bridge.root / "policy.csv").read_text().strip().endswith(",fixed_lots,0.00000001")
    agent.handle("固定0.1手")
    preview = agent.handle("修改 每筆虧10美元")
    assert "每筆停損最多 10.0 帳戶幣別" in preview
    assert not agent.provider.calls


def test_cash_and_fixed_lot_direct_commands_are_not_freeform_questions():
    from aitrader.service import Agent
    assert Agent.risk_request("單筆虧20 USD") == ("risk_amount", 20.0)
    assert Agent.risk_request("每筆固定0.01手") == ("fixed_lots", 0.01)
    assert Agent.risk_request("0.1手") == ("fixed_lots", 0.1)
    assert Agent.risk_request("0.1手配這個停損會虧多少？") is None


def test_risk_only_starts_auto_draft_when_no_policy_exists(agent, snapshot, policy):
    agent.store.set("policy", None)
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    agent.handle("單筆1%")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (agent.store.get("pending"),)).fetchone()
    assert json.loads(row[0])["risk_pct"] == 1.0
    assert agent.provider.calls[-1][0] == "strategy"


def test_ai_strategy_revision_cannot_silently_reset_user_risk(agent, snapshot, policy):
    agent.store.set("policy", policy | {"risk_pct": 1.0, "total_risk_pct": 3.0})
    agent.provider.response = {"policy": policy, "questions": []}
    agent.handle("策略 用 SMC 分析 XAUUSD")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (agent.store.get("pending"),)).fetchone()
    proposed = json.loads(row[0])
    assert proposed["risk_pct"] == 1.0 and proposed["total_risk_pct"] == 3.0
