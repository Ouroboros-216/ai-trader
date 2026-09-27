import json
import time

import pytest

from aitrader.bridge import atomic_write
from aitrader.contracts import DecisionProposal
from aitrader.service import Agent
from aitrader.storage import ProcessLock


def commands(agent):
    return [dict(r) for r in agent.store.db.execute("SELECT * FROM commands ORDER BY created")]


def test_strategy_confirmation_and_restart(agent, policy):
    agent.provider.response = {"policy": policy, "questions": []}
    answer = agent.handle("策略 用 SMC 只做空")
    identifier = agent.store.get("pending")
    assert identifier in answer and agent.version() == 1
    agent.handle("確認 " + identifier)
    assert agent.version() == 2 and agent.store.get("paused")
    other = Agent(agent.cfg, agent.provider)
    try:
        assert other.version() == 2 and other.policy().direction == "SELL"
    finally:
        other.close()
    with pytest.raises(ValueError):
        agent.confirm(identifier)


def test_only_sell_is_enforced_even_if_model_ignores_it(agent, policy):
    agent.provider.response = {"policy": policy | {"direction": "BOTH"}, "questions": []}
    with pytest.raises(ValueError):
        agent.handle("策略 只做空")


def test_stale_proposal(agent):
    identifier = agent.propose("resume", {})
    agent.store.db.execute("UPDATE proposals SET expires=1 WHERE id=?", (identifier,))
    agent.store.db.commit()
    with pytest.raises(ValueError):
        agent.confirm(identifier)


def test_competing_proposals_do_not_both_apply(agent):
    first = agent.propose("resume", {})
    second = agent.propose("resume", {})
    with pytest.raises(ValueError):
        agent.confirm(first)
    agent.confirm(second)
    assert agent.store.get("paused") is False


def test_analysis_queues_valid_commands(agent, decision):
    agent.store.set("paused", False)
    agent.provider.response = {"decisions": [decision]}
    agent.analyze()
    agent.dispatch()
    rows = commands(agent)
    assert len(rows) == 1 and rows[0]["status"] == "sent"
    wire = (agent.bridge.root / "command.csv").read_text()
    assert ",SELL,XAUUSD,0,3010,2980" in wire
    agent.dispatch()
    assert (agent.bridge.root / "command.csv").read_text() == wire


def test_market_watch_removal_stops_new_analysis(agent, snapshot):
    agent.store.set("paused", False)
    snapshot["symbols"].pop("EURUSD")
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    agent.analyze()
    assert agent.provider.calls == []
    assert commands(agent) == []


def test_old_ea_snapshot_disables_new_entries_until_reloaded(agent, snapshot, decision):
    agent.store.set("paused", False)
    snapshot.pop("ea_version")
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    agent.publish()
    assert ",0,SELL," in (agent.bridge.root / "policy.csv").read_text()
    agent.provider.response = {"decisions": [decision]}
    agent.analyze()
    assert not agent.provider.calls
    agent.queue(DecisionProposal(**decision), "ai", time.time())
    agent.dispatch()
    assert commands(agent)[0]["status"] == "cancelled"
    with pytest.raises(ValueError, match="重新掛載"):
        agent.draft("用 SMC")


def test_multi_symbol_batch_is_validated_before_any_queue(agent, decision):
    agent.store.set("paused", False)
    agent.provider.response = {"decisions": [decision, decision | {"action": "BUY"}]}
    with pytest.raises(ValueError):
        agent.analyze()
    assert commands(agent) == []


def test_network_failure_cannot_create_command(agent):
    agent.store.set("paused", False)
    agent.provider.error = TimeoutError("network")
    with pytest.raises(TimeoutError):
        agent.analyze()
    assert not (agent.bridge.root / "command.csv").exists()


def test_pausing_cancels_queued_entries(agent, decision):
    agent.queue(DecisionProposal(**decision), "ai", time.time())
    agent.handle("暫停")
    agent.dispatch()
    assert commands(agent)[0]["status"] == "cancelled"


def test_expired_command_is_not_published(agent, decision):
    agent.store.set("paused", False)
    agent.queue(DecisionProposal(**decision), "ai", time.time(), time.time()-1)
    agent.dispatch()
    assert commands(agent)[0]["status"] == "cancelled"


def test_unacknowledged_command_never_retries_after_restart(agent, decision):
    agent.store.set("paused", False)
    agent.queue(DecisionProposal(**decision), "ai", time.time())
    agent.dispatch()
    agent.store.db.execute("UPDATE commands SET expires=1")
    agent.store.db.commit()
    other = Agent(agent.cfg, agent.provider)
    try:
        other.dispatch()
        assert other.store.get("paused") and commands(other)[0]["status"] == "UNCERTAIN"
    finally:
        other.close()


@pytest.mark.parametrize("status,flat,expected", [("DONE", True, 2), ("DONE", False, 1), ("PARTIAL", True, 1), ("REJECTED", True, 1), ("UNCERTAIN", True, 1)])
def test_reverse_requires_done_and_fresh_flat_snapshot(agent, snapshot, decision, status, flat, expected):
    agent.store.set("paused", False)
    snapshot["positions"] = [{"id": "9", "symbol": "XAUUSD", "owned": True, "side": "BUY", "sl": 2990}]
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    identifier = agent.queue(DecisionProposal(**(decision | {"action": "REVERSE", "reverse_to": "SELL", "position_id": "9"})), "ai", time.time())
    agent.dispatch()
    assert ",CLOSE,XAUUSD,9," in (agent.bridge.root / "command.csv").read_text()
    result = {"id": identifier, "status": status, "retcode": 10009, "detail": "test", "time": int(time.time())-1, "account": "12345", "server": "TEST-Demo"}
    atomic_write(agent.bridge.root / "results.jsonl", json.dumps(result)+"\n")
    agent.ingest()
    if flat:
        snapshot["positions"] = []
    snapshot["time"] = int(time.time())
    atomic_write(agent.bridge.root / "snapshot.json", json.dumps(snapshot))
    agent.reverse_followups()
    agent.reverse_followups()
    assert len(commands(agent)) == expected


def test_result_duplicate_ingest_is_idempotent(agent):
    result = {"id": "abc", "status": "REJECTED", "retcode": 0, "detail": "test", "time": 1, "account": "12345", "server": "TEST-Demo"}
    atomic_write(agent.bridge.root / "results.jsonl", json.dumps(result)+"\n"+json.dumps(result)+"\n")
    agent.ingest()
    agent.ingest()
    assert agent.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='execution'").fetchone()[0] == 1


def test_panel_confirmation_binds_to_displayed_id(agent):
    first = agent.propose("resume", {})
    second = agent.propose("resume", {})
    event = {"id": "ui-1", "account": "12345", "server": "TEST-Demo", "time": int(time.time()), "action": "confirm", "proposal": first}
    atomic_write(agent.bridge.root / "ui.jsonl", json.dumps(event)+"\n")
    agent.panel_events()
    assert agent.store.get("pending") == second and agent.store.get("paused", True)
    assert "未套用" in agent.store.recent(1)[0]["data"]["answer"]


def test_chart_chat_shows_human_readable_status_without_ai_call(agent):
    event = {"id": "chat-1", "account": "12345", "server": "TEST-Demo", "magic": 26092751,
             "time": int(time.time()), "action": "chat", "text": "狀態"}
    atomic_write(agent.bridge.root / "ui.jsonl", json.dumps(event)+"\n")
    agent.panel_events()
    agent.publish()
    shown = (agent.bridge.root / "panel.txt").read_text(encoding="utf8")
    assert "你：狀態" in shown and "目前暫停新單" in shown and "目前沒有持倉" in shown
    assert '"暫停新單":true' not in shown
    assert agent.provider.calls == []


def test_chart_chat_cannot_bypass_confirmation_or_wrong_magic(agent):
    events = [{"id": "bad", "account": "12345", "server": "TEST-Demo", "magic": 999,
               "time": int(time.time()), "action": "chat", "text": "啟動"},
              {"id": "good", "account": "12345", "server": "TEST-Demo", "magic": 26092751,
               "time": int(time.time()), "action": "chat", "text": "啟動"}]
    atomic_write(agent.bridge.root / "ui.jsonl", "".join(json.dumps(e)+"\n" for e in events))
    agent.panel_events()
    assert agent.store.get("paused", True) is True
    assert agent.store.db.execute("SELECT COUNT(*) FROM proposals WHERE kind='resume'").fetchone()[0] == 1


def test_bridge_lock_prevents_two_services(agent):
    with ProcessLock(agent.bridge.root / "test.lock"):
        with pytest.raises(RuntimeError):
            with ProcessLock(agent.bridge.root / "test.lock"):
                pass


def test_database_cannot_be_reused_for_different_account(agent):
    with pytest.raises(ValueError):
        Agent(agent.cfg | {"account": "999"}, agent.provider)
