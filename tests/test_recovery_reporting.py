import json
import time

import pytest

from aitrader.bridge import atomic_write
from aitrader.report import replay, report
from aitrader.telegram import Telegram


def test_partial_audit_write_is_retried(agent):
    data = {"id": "abc", "status": "REJECTED", "retcode": 0, "detail": "test", "time": 1, "account": "12345", "server": "TEST-Demo"}
    path = agent.bridge.root / "results.jsonl"
    path.write_text(json.dumps(data), encoding="utf8")
    agent.ingest()
    assert agent.store.get("results_offset") == 0
    with path.open("a", encoding="utf8") as f:
        f.write("\n")
    agent.ingest()
    assert agent.store.get("results_offset") == path.stat().st_size
    assert agent.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='execution'").fetchone()[0] == 1


def test_truncated_audit_log_blocks_processing(agent):
    agent.store.set("results_offset", 100)
    (agent.bridge.root / "results.jsonl").write_text("", encoding="utf8")
    with pytest.raises(ValueError):
        agent.ingest()


def test_paused_agent_still_manages_position(agent, snapshot):
    snapshot["positions"] = [{"id": "9", "symbol": "XAUUSD", "owned": True, "side": "SELL", "sl": 3010, "volume": .01}]
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    agent.store.set("paused", True)
    agent.provider.response = {"decisions": [{"action": "CLOSE", "symbol": "XAUUSD", "reason": "原理由失效", "position_id": "9"}]}
    agent.analyze()
    agent.dispatch()
    assert ",CLOSE,XAUUSD,9," in (agent.bridge.root / "command.csv").read_text()


def test_missing_market_data_prevents_resume(agent, snapshot):
    snapshot["symbols"]["XAUUSD"]["ready"] = False
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    identifier = agent.propose("resume", {})
    with pytest.raises(ValueError):
        agent.confirm(identifier)


def test_drawdown_halt_prevents_resume(agent, snapshot):
    snapshot["halted"] = True
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    identifier = agent.propose("resume", {})
    with pytest.raises(ValueError):
        agent.confirm(identifier)


def test_cannot_remove_symbol_with_owned_position(agent, snapshot, policy):
    snapshot["positions"] = [{"id": "9", "symbol": "XAUUSD", "owned": True}]
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    identifier = agent.propose("policy", policy | {"symbols": ["EURUSD"]})
    with pytest.raises(ValueError):
        agent.confirm(identifier)
    assert agent.version() == 1


def test_report_aggregates_partial_exits_and_costs(agent):
    for d in [{"position_id": "9", "entry": 0, "volume": .02, "profit": 0, "commission": -1},
              {"position_id": "9", "entry": 1, "volume": .01, "profit": 10, "commission": -.5},
              {"position_id": "9", "entry": 1, "volume": .01, "profit": -2, "commission": -.5}]:
        agent.store.event("deal", d)
    agent.store.event("equity", {"equity": 1000})
    agent.store.event("equity", {"equity": 950})
    result = report(agent.store)
    assert result["closed_positions"] == 1
    assert result["net_closed_profit"] == 6
    assert result["sampled_equity_drawdown_pct"] == 5
    assert result["live_approved"] is False
    assert result["observation_minimum_met"] is False


def test_report_does_not_count_partial_position_as_closed(agent):
    agent.store.event("deal", {"position_id": "9", "entry": 0, "volume": .02})
    agent.store.event("deal", {"position_id": "9", "entry": 1, "volume": .01})
    assert report(agent.store)["closed_positions"] == 0


def test_replay_uses_saved_data_without_provider(agent, policy, snapshot, decision):
    agent.store.event("analysis_input", {"snapshot": snapshot, "policy": policy})
    agent.store.event("decision", {"decision": decision})
    agent.provider.error = AssertionError("network should not be used")
    assert replay(agent.store)["validated_decisions"] == 1


def test_callback_confirmation_and_replay_rejection(agent, monkeypatch):
    monkeypatch.setenv("TG_TEST", "123:testtoken")
    identifier = agent.propose("resume", {})
    sent = []
    def transport(url, body, **kwargs):
        if url.endswith("getUpdates"):
            return {"ok": True, "result": [{"update_id": 55, "callback_query": {"id": "cb", "from": {"id": 7}, "data": identifier,
                    "message": {"chat": {"id": 7, "type": "private"}}}}]}
        sent.append(body)
        return {"ok": True, "result": {}}
    tg = Telegram({"enabled": True, "token_env": "TG_TEST", "user_id": 7, "chat_id": 7}, agent, transport)
    tg.poll()
    assert agent.store.get("paused") is False
    nonce = agent.store.get("resume_nonce")
    tg.poll()
    assert agent.store.get("resume_nonce") == nonce


def test_unauthorized_callback_cannot_confirm(agent, monkeypatch):
    monkeypatch.setenv("TG_TEST", "123:testtoken")
    identifier = agent.propose("resume", {})
    def transport(url, body, **kwargs):
        assert url.endswith("getUpdates")
        return {"ok": True, "result": [{"update_id": 55, "callback_query": {"id": "cb", "from": {"id": 8}, "data": identifier,
                "message": {"chat": {"id": 7, "type": "private"}}}}]}
    Telegram({"enabled": True, "token_env": "TG_TEST", "user_id": 7, "chat_id": 7}, agent, transport).poll()
    assert agent.store.get("paused", True)


def test_memory_does_not_reinclude_full_market_snapshots(agent, snapshot):
    agent.store.event("analysis_input", {"snapshot": snapshot})
    agent.store.event("decision", {"decision": "hold"})
    assert [x["kind"] for x in agent.store.memory()] == ["decision"]
