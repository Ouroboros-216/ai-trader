import json
import time
from pathlib import Path

import pytest

from aitrader.accounts import (load_profiles, new_profile_config, profile_id,
                               read_registry, remove_profile, save_registry, write_mt5_index)
from aitrader.contracts import DecisionProposal, StrategyPolicy
from aitrader.multi import AccountRouter, MultiTelegram, recover_incomplete_batches
from aitrader.provider import Gemini
from aitrader.service import Agent
from aitrader.storage import Store


def config(root, account, server, bridge, database):
    cfg = json.loads((Path(__file__).parents[1] / "config" / "example.json").read_text())
    cfg.update(account=account, server=server, bridge_dir=str(bridge), database=str(database))
    cfg["provider"].update(enabled=True, model="gemini-test", max_calls_per_day=1, min_interval_seconds=1)
    return cfg


def test_multi_profiles_preserve_first_account_and_index(tmp_path):
    (tmp_path / "config").mkdir()
    common = tmp_path / "MetaQuotes" / "Terminal" / "Common" / "Files"
    one = config(tmp_path, "101", "Server-A", common / "AITrader" / "demo-1", tmp_path / "runtime" / "agent.sqlite")
    first = profile_id("101", "Server-A")
    (tmp_path / "config" / "local.json").write_text(json.dumps(one))
    registry = read_registry(tmp_path)
    assert registry["profiles"] == {first: "local.json"}
    second, path, two = new_profile_config(tmp_path, one, "202", "Server-B")
    two["bridge_dir"] = str(common / "AITrader" / second)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(two))
    registry["profiles"][second] = "accounts/" + path.name
    save_registry(tmp_path, registry)
    _, profiles = load_profiles(tmp_path)
    assert profiles[first]["database"] == str(tmp_path / "runtime" / "agent.sqlite")
    assert profiles[second]["database"] != profiles[first]["database"]
    write_mt5_index(profiles)
    rows = (common / "AITrader" / "accounts.txt").read_text().splitlines()
    assert len(rows) == 2 and "101|Server-A" in rows[0] and "202|Server-B" in rows[1]


def test_multi_rejects_duplicate_bridge(tmp_path):
    (tmp_path / "config" / "accounts").mkdir(parents=True)
    one = config(tmp_path, "101", "A", tmp_path / "same", tmp_path / "a.sqlite")
    two = config(tmp_path, "202", "B", tmp_path / "same", tmp_path / "b.sqlite")
    a, b = profile_id("101", "A"), profile_id("202", "B")
    (tmp_path / "config" / "local.json").write_text(json.dumps(one))
    (tmp_path / "config" / "accounts" / (b + ".json")).write_text(json.dumps(two))
    save_registry(tmp_path, {"version": 1, "profiles": {a: "local.json", b: "accounts/" + b + ".json"}, "shared_profile": a})
    with pytest.raises(ValueError, match="重複"):
        load_profiles(tmp_path)


def test_remove_profile_keeps_audit_data_and_reassigns_shared_bot(tmp_path, monkeypatch):
    (tmp_path / "config" / "accounts").mkdir(parents=True)
    common = tmp_path / "MetaQuotes" / "Terminal" / "Common" / "Files"
    a, b = profile_id("101", "A"), profile_id("202", "B")
    one = config(tmp_path, "101", "A", common / "AITrader" / a, tmp_path / "a.sqlite")
    two = config(tmp_path, "202", "B", common / "AITrader" / b, tmp_path / "b.sqlite")
    one["provider"]["model"] = "shared-model"
    two["provider"]["model"] = "old-model"
    (tmp_path / "config" / "local.json").write_text(json.dumps(one))
    (tmp_path / "config" / "accounts" / (b + ".json")).write_text(json.dumps(two))
    save_registry(tmp_path, {"version": 1, "profiles": {a: "local.json", b: "accounts/" + b + ".json"}, "shared_profile": a})
    _, profiles = load_profiles(tmp_path)
    write_mt5_index(profiles)
    first_store = Store(tmp_path / "a.sqlite")
    first_store.set("policy", {"version": 1})
    first_store.db.close()
    Path(profiles[a]["bridge_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(profiles[a]["bridge_dir"]) / "snapshot.json").write_text(json.dumps({
        "account": "101", "server": "A", "magic": one["magic"], "demo": True,
        "time": int(time.time()), "positions": []}))
    monkeypatch.setattr("aitrader.accounts.ea_is_running", lambda _: False)
    assert remove_profile(tmp_path, a) == b
    registry, loaded = load_profiles(tmp_path)
    assert list(loaded) == [b] and registry["shared_profile"] == b
    assert loaded[b]["provider"]["model"] == "shared-model"
    assert (tmp_path / "config" / "local.json").exists()
    assert (tmp_path / "a.sqlite").exists()
    assert "101|A" not in (common / "AITrader" / "accounts.txt").read_text()
    retained = Store(tmp_path / "a.sqlite")
    assert retained.get("paused") is True
    retained.db.close()


def test_remove_profile_refuses_active_ea_or_owned_position(tmp_path, monkeypatch):
    (tmp_path / "config" / "accounts").mkdir(parents=True)
    common = tmp_path / "MetaQuotes" / "Terminal" / "Common" / "Files"
    a, b = profile_id("101", "A"), profile_id("202", "B")
    one = config(tmp_path, "101", "A", common / "AITrader" / a, tmp_path / "a.sqlite")
    two = config(tmp_path, "202", "B", common / "AITrader" / b, tmp_path / "b.sqlite")
    (tmp_path / "config" / "local.json").write_text(json.dumps(one))
    (tmp_path / "config" / "accounts" / (b + ".json")).write_text(json.dumps(two))
    save_registry(tmp_path, {"version": 1, "profiles": {a: "local.json", b: "accounts/" + b + ".json"}, "shared_profile": a})
    monkeypatch.setattr("aitrader.accounts.ea_is_running", lambda _: True)
    with pytest.raises(ValueError, match="EA 仍掛"):
        remove_profile(tmp_path, a)
    monkeypatch.setattr("aitrader.accounts.ea_is_running", lambda _: False)
    (common / "AITrader" / a).mkdir(parents=True, exist_ok=True)
    (common / "AITrader" / a / "snapshot.json").write_text(json.dumps({
        "account": "101", "server": "A", "magic": one["magic"], "demo": True,
        "time": int(time.time()), "positions": [{"owned": True}]}))
    with pytest.raises(ValueError, match="仍有本系統持倉"):
        remove_profile(tmp_path, a)
    assert len(read_registry(tmp_path)["profiles"]) == 2


class FakeAgent:
    def __init__(self, account, path):
        self.cfg = {"account": account, "server": "Demo"}
        self.store = Store(path)
        self.messages = []

    def handle(self, message):
        self.messages.append(message)
        if message == "狀態":
            raise ValueError("MT5 快照已過期")
        if message == "啟動":
            self.store.set("pending", "a" * 32)
            return "啟動確認 " + "a" * 32
        return "ok"


def test_telegram_router_keeps_account_with_confirmation(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    assert "請先" in router.handle("啟動")
    assert not a.messages and not b.messages
    router.handle("帳號 demo-a")
    assert "確認" in router.handle("啟動")
    callback = router.callback_data("a" * 32)
    router.handle("帳號 demo-b")
    router.handle("確認 " + callback)
    assert a.messages[-1] == "確認 " + "a" * 32
    assert not b.messages


def test_router_error_identifies_account(tmp_path):
    agent = FakeAgent("101", tmp_path / "a.sqlite")
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    with pytest.raises(ValueError, match="帳號 Demo｜101：MT5 快照已過期"):
        router.handle("狀態")


def test_router_accepts_bare_account_number_alias_server_and_row(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    a.cfg["server"], b.cfg["server"] = "Broker-A", "Broker-B"
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    assert "已選擇 Broker-A｜101" in router.handle("101")
    assert "已選擇 Broker-B｜202" in router.handle("demo-b")
    assert "已選擇 Broker-A｜101" in router.handle("Broker-A")
    assert "已選擇 Broker-B｜202" in router.handle("demo-b：202 @ Broker-B")
    assert "已選擇 Broker-B｜202" in router.handle("Broker-B｜202")
    assert "→ Broker-B｜202" in router.handle("帳號")


def test_router_rejects_ambiguous_bare_account(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("101", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    with pytest.raises(ValueError, match="不唯一"):
        router.handle("101")
    assert not a.messages and not b.messages


def test_chinese_account_menu_and_back_remove_scope(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    opening = router.handle("帳號")
    choices = router.reply_markup("帳號", opening, "")["inline_keyboard"]
    assert [row[0]["text"] for row in choices] == ["Demo｜101", "Demo｜202"]
    select = router.callback_message(choices[0][0]["callback_data"])
    selected = router.handle(select)
    labels = [button["text"] for row in router.reply_markup(select, selected, "")["inline_keyboard"] for button in row]
    assert labels == ["狀態", "原因", "查看策略", "討論／修改策略", "自動模式", "全部策略", "交易管理", "返回帳號清單"]
    auto = next(button["callback_data"] for row in router.reply_markup(select, selected, "")["inline_keyboard"]
                for button in row if button["text"] == "自動模式")
    assert router.handle(router.callback_message(auto)) == "【Demo｜101】\nok"
    assert a.messages[-1] == "自動模式" and not b.messages
    back = router.reply_markup(select, selected, "")["inline_keyboard"][-1][0]["callback_data"]
    assert router.callback_message(back) == "返回"
    assert "未選擇" in router.handle("返回")
    assert router.selected() == ""
    assert "請先" in router.handle("狀態")
    assert a.messages == ["自動模式"] and not b.messages


def test_proposal_confirmation_button_stays_account_bound(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    router.handle("101")
    reply = router.handle("啟動")
    rows = router.reply_markup("啟動", reply, router.store.get("pending"))["inline_keyboard"]
    assert rows[0][0]["text"] == "確認此提案"
    callback = rows[0][0]["callback_data"]
    router.handle("202")
    router.handle(router.callback_message(callback))
    assert a.messages[-1] == "確認 " + "a" * 32
    assert not b.messages


def test_reject_button_routes_to_original_account_after_selection_changes(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    router.handle("101")
    proposal = "b" * 32
    callback = "r:" + router.account_token("demo-a") + ":" + proposal
    router.handle("202")
    assert router.handle(router.callback_message(callback)) == "【Demo｜101】\nok"
    assert a.messages == ["拒絕草案 " + proposal] and not b.messages


def test_reject_callback_rejects_malformed_account_or_proposal(tmp_path):
    router = AccountRouter({"demo-a": FakeAgent("101", tmp_path / "a.sqlite")}, Store(tmp_path / "router.sqlite"))
    with pytest.raises(ValueError, match="草案按鈕"):
        router.callback_message("r:short:abc")
    with pytest.raises(ValueError, match="帳號按鈕"):
        router.callback_message("r:badbadbadbad:" + "a" * 32)


def test_old_menu_button_cannot_act_after_switch(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    router.handle("101")
    old = router.reply_markup("101", "已選擇", "")["inline_keyboard"][2][0]["callback_data"]
    router.handle("202")
    with pytest.raises(ValueError, match="已切換帳號"):
        router.callback_message(old)
    assert not a.messages and not b.messages


def test_menu_shows_only_contextual_common_actions(agent, tmp_path, monkeypatch):
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    router.handle("12345")
    def labels(command):
        return [button["text"] for row in router.reply_markup(command, "", "")["inline_keyboard"] for button in row]
    normal = labels("帳號 12345")
    assert {"狀態", "原因", "查看策略", "討論／修改策略", "交易管理"} <= set(normal)
    assert not {"暫停", "啟動", "平倉", "重設回撤"} & set(normal)
    assert "啟動" in labels("交易管理") and "平倉" not in labels("交易管理")
    assert "返回主選單" in labels("交易管理")
    assert "已返回" in router.handle("返回主選單") and router.selected() == "demo-a"
    agent.store.set("paused", False)
    assert "暫停" in labels("交易管理") and "啟動" not in labels("交易管理")
    monkeypatch.setattr(agent, "snapshot", lambda: {"positions": [{"owned": True}], "halted": True})
    assert {"暫停", "平倉"} <= set(labels("交易管理"))
    assert "重設回撤" not in labels("交易管理")  # Open positions make reset inapplicable.
    assert "持倉" in labels("狀態")
    monkeypatch.setattr(agent, "snapshot", lambda: {"positions": [], "halted": True, "total_halt": True})
    assert "重設回撤" in labels("交易管理")
    agent.store.set("paused", True)
    assert "啟動" not in labels("交易管理")
    monkeypatch.setattr(agent, "snapshot", lambda: {"positions": [], "halted": True, "total_halt": False})
    assert "重設回撤" not in labels("交易管理")  # A daily halt alone cannot reset drawdown.
    monkeypatch.setattr(agent, "pending_policy", lambda: ("x" * 32, {"title": "草案"}))
    assert "查看草案" in labels("狀態")


def test_choosing_ai_option_is_local_and_draft_is_account_bound(agent, policy, tmp_path):
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    router.handle("12345")
    agent.provider.response = {"answer": "XAUUSD 採 M5 短線，討論每筆 1% 風險。\n\n方案A：維持 0.5%\n- 先觀察。\n\n方案B：每筆 1% 做 M5 短線\n- 持倉約 5–15 分鐘。"}
    discussed = router.handle("比較短線方案")
    assert "方案B" in discussed and len(agent.provider.calls) == 1
    rows = router.reply_markup("比較短線方案", discussed, "")["inline_keyboard"]
    option_button = next(button for row in rows for button in row if button["text"].startswith("方案 B"))
    assert "已選方案 B" in router.handle(router.callback_message(option_button["callback_data"]))
    assert len(agent.provider.calls) == 1  # Selecting an option never calls AI or changes policy.
    assert "整理選定方案" in [button["text"] for row in router.reply_markup("選方案 B", "", "")["inline_keyboard"] for button in row]
    agent.provider.response = {"policy": policy | {"title": "M5 短線", "risk_pct": 1.0}, "questions": []}
    preview = router.handle("整理選定方案")
    assert "策略草案" in preview and "單筆 1.0%" in preview
    assert agent.provider.calls[-1][0] == "strategy"
    assert "方案 B" in agent.provider.calls[-1][1]["request"]
    assert agent.store.get("policy") == policy and agent.store.get("paused", True) is True
    assert router.store.get("pending") == agent.store.get("pending")


def test_selected_option_rejects_wrong_model_timeframe_before_creating_proposal(agent, policy, tmp_path):
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    router.handle("12345")
    agent.provider.response = {"answer": "討論 XAUUSD 每筆 1%。\n\n方案A：M15 波段\n\n方案B：M5 短線，每筆 1%"}
    router.handle("比較")
    router.handle("B")
    agent.provider.response = {"policy": policy | {"timeframes": ["M15", "H1", "H4"]}, "questions": []}
    with pytest.raises(ValueError, match="分析週期"):
        router.handle("整理選定方案")
    assert agent.store.db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
    assert agent.store.get("policy") == policy


def test_new_discussion_invalidates_old_option_selection(agent, tmp_path):
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    router.handle("12345")
    agent.provider.response = {"answer": "方案A：舊方法\n\n方案B：舊方法二"}
    router.handle("比較")
    router.handle("B")
    agent.provider.response = {"answer": "我們改談其他問題。"}
    router.handle("那商品規格呢？")
    assert "沒有有效" in router.handle("B")
    assert "已過期" in router.handle("整理選定方案")


def test_stale_or_other_account_option_button_is_rejected(tmp_path):
    a, b = FakeAgent("101", tmp_path / "a.sqlite"), FakeAgent("202", tmp_path / "b.sqlite")
    router = AccountRouter({"demo-a": a, "demo-b": b}, Store(tmp_path / "router.sqlite"))
    router.handle("101")
    router.remember_options("demo-a", "方案A：先觀察\n\n方案B：短線")
    callback = next(button["callback_data"] for row in router.reply_markup("狀態", "", "")["inline_keyboard"]
                    for button in row if button["text"].startswith("方案 B"))
    router.handle("202")
    with pytest.raises(ValueError, match="已切換帳號"):
        router.callback_message(callback)
    router.handle("101")
    router.remember_options("demo-a", "方案A：新觀察\n\n方案B：新短線")
    with pytest.raises(ValueError, match="已過期"):
        router.callback_message(callback)


def test_telegram_menu_callback_and_private_chinese_slash_descriptions(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_TEST", "123:testtoken")
    agent = FakeAgent("101", tmp_path / "a.sqlite")
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    sent = []
    updates = [
        {"update_id": 1, "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "date": __import__("time").time(), "text": "帳號"}},
        {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": 7}, "data": "a:" + router.account_token("demo-a"), "message": {"chat": {"id": 7, "type": "private"}}}},
    ]
    def transport(url, body, **_):
        method = url.rsplit("/", 1)[-1]
        if method == "getUpdates":
            return {"ok": True, "result": [updates.pop(0)] if updates else []}
        sent.append((method, body))
        return {"ok": True, "result": True}
    bot = MultiTelegram({"enabled": True, "token_env": "TG_TEST", "user_id": 7, "chat_id": 7}, router, transport)
    bot.install_commands()
    assert sent[0][0] == "setMyCommands"
    assert sent[0][1]["scope"] == {"type": "chat", "chat_id": 7}
    assert all(command["description"] for command in sent[0][1]["commands"])
    bot.poll()
    assert sent[-1][1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Demo｜101"
    bot.poll()
    assert router.selected() == "demo-a"
    menu = next(body for method, body in sent if method == "sendMessage" and "已選擇" in body["text"])
    assert any(button["text"] == "返回帳號清單" for row in menu["reply_markup"]["inline_keyboard"] for button in row)


def test_unauthorized_menu_callback_does_not_select_account(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_TEST", "123:testtoken")
    router = AccountRouter({"demo-a": FakeAgent("101", tmp_path / "a.sqlite")}, Store(tmp_path / "router.sqlite"))
    def transport(url, body, **_):
        if url.endswith("getUpdates"):
            return {"ok": True, "result": [{"update_id": 1, "callback_query": {
                "id": "cb", "from": {"id": 8}, "data": "a:" + router.account_token("demo-a"),
                "message": {"chat": {"id": 7, "type": "private"}}}}]}
        raise AssertionError("unauthorized callback must not send a reply")
    MultiTelegram({"enabled": True, "token_env": "TG_TEST", "user_id": 7, "chat_id": 7}, router, transport).poll()
    assert router.store.get("selected_account") is None


def test_one_batch_strategy_applies_to_all_paused_accounts(agent, policy, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        other.bridge.root.joinpath("snapshot.json").write_text(json.dumps(
            agent.snapshot() | {"account": "67890"}))
        agent.provider.response = {"policy": policy, "questions": []}
        router = AccountRouter({"demo-a": agent, "demo-b": other}, Store(tmp_path / "router.sqlite"))
        preview = router.handle("策略全部 用 SMC 只做空")
        proposal = router.store.get("pending")
        assert "確認 all|" + proposal in preview
        assert agent.version() == 1 and other.version() == 0
        markup = router.reply_markup("策略全部 用 SMC 只做空", preview, proposal)
        assert markup["inline_keyboard"][0][0]["text"] == "確認套用全部策略"
        assert "2 個帳號" in router.handle(router.callback_message(markup["inline_keyboard"][0][0]["callback_data"]))
        assert agent.version() == 2 and other.version() == 1
        assert agent.policy().direction == other.policy().direction == "SELL"
        assert agent.store.get("paused") and other.store.get("paused")
        with pytest.raises(ValueError, match="已過期或已使用"):
            router.handle("確認 all|" + proposal)
    finally:
        other.close()


def test_batch_strategy_does_not_change_existing_account_risk(agent, policy, snapshot, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        other.store.set("policy", policy | {"risk_pct": 0.2, "total_risk_pct": 0.8})
        other.bridge.root.joinpath("snapshot.json").write_text(json.dumps(snapshot | {"account": "67890"}))
        agent.provider.response = {"policy": policy, "questions": []}
        router = AccountRouter({"demo-a": agent, "demo-b": other}, Store(tmp_path / "router.sqlite"))
        router.handle("策略全部 用 SMC 只做空")
        router.handle("確認 all|" + router.store.get("pending"))
        assert agent.policy().risk_pct == 0.5
        assert other.policy().risk_pct == 0.2 and other.policy().total_risk_pct == 0.8
    finally:
        other.close()


def test_batch_maps_broker_suffix_per_account(agent, policy, snapshot, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        actual = dict(snapshot["symbols"])
        actual["XAUUSD.a"] = actual.pop("XAUUSD")
        actual["EURUSD.a"] = actual.pop("EURUSD")
        other.bridge.root.joinpath("snapshot.json").write_text(json.dumps(
            snapshot | {"account": "67890", "symbols": actual, "missing_symbols": []}))
        agent.provider.response = {"policy": policy, "questions": []}
        router = AccountRouter({"demo-a": agent, "demo-b": other}, Store(tmp_path / "router.sqlite"))
        assert "EA 快照" in router.handle("策略全部 用 SMC 只做空")
        router.handle("確認 all|" + router.store.get("pending"))
        assert agent.policy().symbols == ["XAUUSD", "EURUSD"]
        assert other.policy().symbols == ["XAUUSD.a", "EURUSD.a"]
    finally:
        other.close()


def test_batch_maps_new_symbol_from_each_broker_catalog(agent, policy, snapshot, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        broker_snapshot = snapshot | {"symbols": {"BTCUSD.a": snapshot["symbols"]["XAUUSD"]}}
        agent.bridge.root.joinpath("snapshot.json").write_text(json.dumps(broker_snapshot))
        other.bridge.root.joinpath("snapshot.json").write_text(json.dumps(
            snapshot | {"account": "67890", "symbols": {"BTCUSDm": snapshot["symbols"]["XAUUSD"]}}))
        for item, symbols in ((agent, ["BTCUSD.a"]), (other, ["BTCUSDm"])):
            item.bridge.root.joinpath("catalog.json").write_text(json.dumps({
                "account": item.cfg["account"], "server": item.cfg["server"], "magic": item.cfg["magic"],
                "demo": True, "time": int(time.time()), "symbols": symbols}))
        agent.provider.response = {"policy": policy | {"symbols": ["BTCUSD"]}, "questions": []}
        router = AccountRouter({"demo-a": agent, "demo-b": other}, Store(tmp_path / "router.sqlite"))
        preview = router.handle("策略全部 BTCUSD 只做空")
        assert "BTCUSD.a" in preview and "BTCUSDm" in preview
        router.handle("確認 all|" + router.store.get("pending"))
        assert agent.policy().symbols == ["BTCUSD.a"]
        assert other.policy().symbols == ["BTCUSDm"]
        assert agent.store.get("paused") and other.store.get("paused")
    finally:
        other.close()


def test_existing_offline_account_blocks_batch_without_partial_apply(agent, policy, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        other.store.set("policy", policy)
        agent.provider.response = {"policy": policy, "questions": []}
        router = AccountRouter({"demo-a": agent, "demo-b": other}, Store(tmp_path / "router.sqlite"))
        with pytest.raises(ValueError, match="沒有共同商品"):
            router.handle("策略全部 用 SMC 只做空")
        assert agent.provider.calls == []
        assert router.store.get("pending") is None
        assert agent.version() == 1 and other.version() == 1
    finally:
        other.close()


def test_interrupted_batch_recovery_pauses_every_account(agent, policy, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        other.bridge.root.joinpath("snapshot.json").write_text(json.dumps(
            agent.snapshot() | {"account": "67890"}))
        agent.provider.response = {"policy": policy, "questions": []}
        router = AccountRouter({"demo-a": agent, "demo-b": other}, Store(tmp_path / "router.sqlite"))
        router.handle("策略全部 用 SMC 只做空")
        with router.store.db:
            router.store.db.execute("UPDATE proposals SET status='applying' WHERE id=?", (router.store.get("pending"),))
        agent.store.set("paused", False)
        other.store.set("paused", False)
        recover_incomplete_batches(router.agents, router.store)
        assert agent.store.get("paused") and other.store.get("paused")
        assert router.store.db.execute("SELECT status FROM proposals").fetchone()[0] == "partial"
    finally:
        other.close()


def test_batch_rejects_model_direction_that_conflicts_with_short_only(agent, policy, tmp_path):
    agent.cfg["ea"] = {"symbols": "XAUUSD,EURUSD"}
    agent.provider.response = {"policy": policy | {"direction": "BOTH"}, "questions": []}
    router = AccountRouter({"demo-a": agent}, Store(tmp_path / "router.sqlite"))
    with pytest.raises(ValueError, match="未遵守只做空"):
        router.handle("策略全部 只做空")
    assert agent.version() == 1
    assert router.store.db.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0


def test_execution_context_is_account_specific_and_sent_to_decision_model(agent, policy, decision, tmp_path):
    command = DecisionProposal.parse(decision, StrategyPolicy.parse(policy, 1), agent.snapshot())
    identifier = agent.queue(command, "test", time.time())
    agent.store.event("execution", {"id": identifier, "status": "REJECTED", "latency_ms": 420})
    with agent.store.db:
        agent.store.db.execute("UPDATE commands SET status='REJECTED' WHERE id=?", (identifier,))
    other_cfg = agent.cfg | {"bridge_dir": str(tmp_path / "other-bridge"),
                             "database": str(tmp_path / "other.sqlite"), "account": "67890"}
    other = Agent(other_cfg, agent.provider)
    try:
        assert other.execution_context() == {}
        assert agent.execution_context() == {"XAUUSD": {"samples": 1, "rejections": 1,
                                                        "median_latency_ms": 420, "p90_latency_ms": 420}}
        agent.store.set("paused", False)
        agent.provider.response = {"decisions": []}
        agent.analyze()
        kind, payload = agent.provider.calls[-1]
        assert kind == "decisions"
        assert payload["execution_context"]["XAUUSD"]["median_latency_ms"] == 420
        assert payload["snapshot"]["account"] == "12345"
    finally:
        other.close()


def test_shared_api_interval_applies_to_multiple_account_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-key")
    quota, a, b = (Store(tmp_path / name) for name in ("quota.sqlite", "a.sqlite", "b.sqlite"))
    cfg = {"enabled": True, "model": "gemini-test", "api_key_env": "GEMINI_API_KEY",
           "max_calls_per_day": 1, "min_interval_seconds": 1, "timeout_seconds": 1,
           "max_output_tokens": 128}
    response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": '{"answer":"ok"}'}]}}]}
    transport = lambda *_: response
    assert Gemini(cfg, a, transport, quota).call("chat", {}) == {"answer": "ok"}
    with pytest.raises(ValueError, match="cooldown"):
        Gemini(cfg, b, transport, quota).call("chat", {})
    assert a.recent()[-1]["kind"] == "provider_response"
    assert not b.recent()


def test_shared_cooldown_without_local_daily_limit(tmp_path):
    store = Store(tmp_path / "quota.sqlite")
    cfg = {"model": "gemini-test", "max_calls_per_day": 2, "min_interval_seconds": 15}
    store.reserve_call("decisions", cfg, 1000)
    with pytest.raises(ValueError, match="cooldown"):
        store.reserve_call("decisions", cfg, 1001)
    store.reserve_call("decisions", cfg, 1016)
    store.reserve_call("decisions", cfg, 1032)
