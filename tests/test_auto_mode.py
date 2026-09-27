import json

import pytest

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


def test_invalid_auto_strategy_never_creates_proposal(agent, snapshot):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": ["buy everything"], "questions": []}
    with pytest.raises(ValueError, match="格式不正確"):
        agent.handle("自動模式")
    assert agent.store.get("pending", "") == ""


def test_risk_only_message_starts_auto_draft_with_requested_risk(agent, snapshot, policy):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    agent.handle("風險0.3%")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (agent.store.get("pending"),)).fetchone()
    assert json.loads(row[0])["risk_pct"] == 0.3
    with pytest.raises(ValueError, match="0.5%"):
        agent.handle("自動模式 每筆風險1%")
    assert len(agent.provider.calls) == 1


def test_auto_mode_total_risk_example_keeps_per_trade_limit(agent, snapshot, policy):
    ready_bars(agent, snapshot)
    agent.provider.response = {"policy": policy, "questions": []}
    agent.handle("自動模式 總風險1%")
    row = agent.store.db.execute("SELECT data FROM proposals WHERE id=?", (agent.store.get("pending"),)).fetchone()
    proposed = json.loads(row[0])
    assert proposed["risk_pct"] == 0.5 and proposed["total_risk_pct"] == 1.0
