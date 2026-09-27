import json
import time

import pytest

from aitrader.symbols import account_mapping, ambiguous_choice, canonical, relevant


def test_broker_suffix_groups_and_explicit_selection():
    assert canonical("XAUUSD.a") == canonical("XAUUSDm") == "XAUUSD"
    assert canonical("BTCUSDT") != canonical("BTCUSD")
    assert "XAUUSD" not in account_mapping(["XAUUSD.a", "XAUUSD.b"])
    assert account_mapping(["XAUUSD.a"])["XAUUSD"] == "XAUUSD.a"
    assert ambiguous_choice("XAUUSD.a", "用黃金只做空", ["XAUUSD.a", "XAUUSD.b"])
    assert not ambiguous_choice("XAUUSD.a", "用 XAUUSD.a 只做空", ["XAUUSD.a", "XAUUSD.b"])


def test_strategy_requires_symbol_visible_in_market_watch(agent, policy):
    agent.bridge.root.joinpath("catalog.json").write_text(json.dumps({
        "account": agent.cfg["account"], "server": agent.cfg["server"], "magic": agent.cfg["magic"],
        "demo": True, "time": int(time.time()), "symbols": ["BTCUSD.a", "XAUUSD"]}))
    agent.provider.response = {"policy": policy | {"symbols": ["BTCUSD.a"]}, "questions": []}
    with pytest.raises(ValueError, match="策略包含不在券商商品清單內"):
        agent.draft("用 BTCUSD.a 只做空")
    snapshot = agent.snapshot()
    snapshot["symbols"]["BTCUSD.a"] = snapshot["symbols"]["XAUUSD"] | {"ready": False}
    agent.bridge.root.joinpath("snapshot.json").write_text(json.dumps(snapshot))
    preview = agent.draft("用 BTCUSD.a 只做空")
    assert "BTCUSD.a" in preview
    assert "已套用" in agent.confirm(agent.store.get("pending"))
    assert agent.policy().symbols == ["BTCUSD.a"]
    assert agent.store.get("paused") is True
    try:
        agent.confirm(agent.propose("resume", {}))
    except ValueError as exc:
        assert "行情尚未就緒" in str(exc)
    else:
        raise AssertionError("new symbol must wait until EA publishes complete bars")


def test_ambiguous_suffix_requires_exact_name_before_proposal(agent, policy):
    agent.bridge.root.joinpath("catalog.json").write_text(json.dumps({
        "account": agent.cfg["account"], "server": agent.cfg["server"], "magic": agent.cfg["magic"],
        "demo": True, "time": int(time.time()), "symbols": ["XAUUSD.a", "XAUUSD.b"]}))
    agent.provider.response = {"policy": policy | {"symbols": ["XAUUSD.a"]}, "questions": []}
    snapshot = agent.snapshot()
    snapshot["symbols"] = {"XAUUSD.a": snapshot["symbols"]["XAUUSD"],
                           "XAUUSD.b": snapshot["symbols"]["XAUUSD"]}
    agent.bridge.root.joinpath("snapshot.json").write_text(json.dumps(snapshot))
    reply = agent.draft("用黃金只做空")
    assert "需要選擇" in reply and "XAUUSD.a" in reply and "XAUUSD.b" in reply
    assert agent.store.get("pending") is None


def test_relevant_catalog_searches_unlisted_broker_symbols():
    available = ["AAPL.US", "EURUSD.a", "XAUUSD.a", "XAUUSD.b", "BTCUSDm"]
    assert relevant(available, "用黃金做空") == ["XAUUSD.a", "XAUUSD.b"]
    assert relevant(available, "BTCUSD 只做多") == ["BTCUSDm"]
