import json
import time

import pytest

from aitrader.bridge import atomic_write
from aitrader.contracts import StrategyPolicy
from aitrader.watch import WatchCandidate
from aitrader.storage import Store


def candidate(decision, **changes):
    return {"symbol": "XAUUSD", "basis": "QUOTE", "timeframe": "M15",
            "trigger_operator": "BELOW", "trigger_price": 2999.0,
            "invalidation_operator": "ABOVE", "invalidation_price": 3005.0,
            "expires": int(time.time()) + 3600, "reason": "等待 M15 空頭條件",
            "decision": decision} | changes


def test_local_watch_triggers_once_then_requires_existing_ai_entry_review(agent, snapshot, policy, decision):
    agent.store.set("paused", False)
    obj = WatchCandidate.parse(candidate(decision), StrategyPolicy.parse(policy, 1), snapshot, int(time.time()))
    agent.store.set("watch_candidates", {obj.symbol: obj.to_dict()})
    snapshot["symbols"]["XAUUSD"] |= {"bid": 2998.0, "ask": 2998.2}
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    agent.monitor_watches()
    agent.monitor_watches()
    assert not agent.store.get("watch_candidates")
    rows = agent.store.db.execute("SELECT parent,status FROM commands").fetchall()
    assert len(rows) == 1 and rows[0]["parent"] == "watch" and rows[0]["status"] == "queued"
    assert not agent.provider.calls
    agent.provider.response = {"allow": True, "reason": "最新條件仍成立"}
    agent.dispatch()
    assert agent.provider.calls[-1][0] == "entry_review"


def test_invalidation_cancels_old_setup_before_ai_replanning(agent, snapshot, policy, decision):
    agent.store.set("paused", False)
    obj = WatchCandidate.parse(candidate(decision), StrategyPolicy.parse(policy, 1), snapshot, int(time.time()))
    agent.store.set("watch_candidates", {obj.symbol: obj.to_dict()})
    agent.store.set("last_analysis", time.time())
    snapshot["symbols"]["XAUUSD"] |= {"bid": 3005.8, "ask": 3006.0}
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    agent.monitor_watches()
    assert agent.store.get("watch_candidates") == {}
    assert agent.store.get("last_analysis") == 0
    assert agent.store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    assert not agent.provider.calls


def test_watch_rejects_already_triggered_and_wrong_direction(snapshot, policy, decision):
    p = StrategyPolicy.parse(policy, 1)
    with pytest.raises(ValueError, match="already reached"):
        WatchCandidate.parse(candidate(decision, trigger_price=3001), p, snapshot, int(time.time()))
    with pytest.raises(ValueError, match="direction forbidden"):
        WatchCandidate.parse(candidate(decision | {"action": "BUY"}), p, snapshot, int(time.time()))


def test_completed_bar_watch_rejects_a_condition_already_met(snapshot, policy, decision):
    snapshot["symbols"]["XAUUSD"]["bars"] = {"M15": [{"time": 123, "close": 3000.0}]}
    with pytest.raises(ValueError, match="already reached"):
        WatchCandidate.parse(candidate(decision, basis="CLOSE", trigger_operator="ABOVE",
                                       trigger_price=2999.0), StrategyPolicy.parse(policy, 1),
                             snapshot, int(time.time()))


def test_ai_creates_watch_and_does_not_recheck_while_all_symbols_are_watched(agent, snapshot, policy, decision):
    agent.store.set("paused", False)
    agent.store.set("policy", policy | {"symbols": ["XAUUSD"]})
    agent.provider.response = {"decisions": [], "watches": [candidate(decision)]}
    agent.analyze()
    assert agent.store.get("watch_candidates")["XAUUSD"]["trigger_price"] == 2999.0
    assert [kind for kind, _ in agent.provider.calls] == ["decisions"]
    agent.store.set("last_analysis", 0)
    agent.analyze()
    assert [kind for kind, _ in agent.provider.calls] == ["decisions"]


def test_ai_snapshot_only_sends_policy_symbols_and_bounded_bars(agent, snapshot, policy):
    from aitrader.service import Agent
    rows = [{"time": n, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 3} for n in range(100)]
    snapshot["symbols"]["XAUUSD"]["bars"] = {tf: rows for tf in ("M5", "M15", "H1", "H4")}
    snapshot["symbols"]["BTCUSD"] = snapshot["symbols"]["XAUUSD"]
    compact = Agent.ai_snapshot(snapshot, StrategyPolicy.parse(policy, 1))
    assert "BTCUSD" not in compact["symbols"]
    assert {tf: len(bars) for tf, bars in compact["symbols"]["XAUUSD"]["bars"].items()} == {
        "M5": 20, "M15": 24, "H1": 16, "H4": 10}
    assert len(compact["symbols"]["XAUUSD"]["bars"]["M15"][0]) == 6


def test_daily_token_gate_counts_gemini_and_openai_usage(tmp_path):
    store = Store(tmp_path / "quota.sqlite")
    now = time.time()
    with store.db:
        store.db.execute("INSERT INTO calls(time,kind,model,status,usage) VALUES(?,?,?,?,?)",
                         (now, "decisions", "gemini", "ok", '{"totalTokenCount":120}'))
        store.db.execute("INSERT INTO calls(time,kind,model,status,usage) VALUES(?,?,?,?,?)",
                         (now, "chat", "openai", "ok", '{"total_tokens":80}'))
    cfg = {"model": "test", "max_calls_per_day": 10, "max_tokens_per_day": 200,
           "min_interval_seconds": 0}
    with pytest.raises(ValueError, match="token"):
        store.reserve_call("decisions", cfg, now + 1)
    store.db.close()
