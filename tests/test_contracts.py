import math
import time

import pytest

from aitrader.contracts import DecisionProposal, ExecutionResult, MarketSnapshot, StrategyPolicy


def test_valid_sell(policy, snapshot, decision):
    assert DecisionProposal.parse(decision, StrategyPolicy.parse(policy, 1), snapshot).action == "SELL"


@pytest.mark.parametrize("change", [
    {"action": "BUY", "sl": 2990, "tp": 3020}, {"symbol": "BTCUSD"}, {"sl": 0}, {"tp": math.nan},
    {"sl": "3010"}, {"sl": True}, {"action": "EXECUTE_CODE"}, {"lots": 10}, {"tp": 3020},
    {"position_id": "x"}, {"reason": ""}, {"management": ""}])
def test_bad_decisions_rejected(policy, snapshot, decision, change):
    with pytest.raises((ValueError, TypeError)):
        DecisionProposal.parse(decision | change, StrategyPolicy.parse(policy, 1), snapshot)


@pytest.mark.parametrize("field,value", [("risk_pct", 101), ("risk_pct", 0), ("total_risk_pct", 101), ("daily_loss_pct", 101), ("drawdown_pct", 101), ("risk_pct", math.inf)])
def test_policy_hard_limits(policy, field, value):
    with pytest.raises(ValueError):
        StrategyPolicy.parse(policy | {field: value}, 1)


def test_user_selected_percentages_above_initial_defaults_are_valid(policy):
    selected = policy | {"risk_pct": 1, "total_risk_pct": 3, "daily_loss_pct": 4, "drawdown_pct": 10}
    assert StrategyPolicy.parse(selected, 2).to_dict()["risk_pct"] == 1
    with pytest.raises(ValueError):
        StrategyPolicy.parse(selected | {"total_risk_pct": 0.5}, 2)


def test_cash_and_fixed_lot_modes_are_exclusive(policy):
    assert StrategyPolicy.parse(policy | {"risk_mode": "cash", "risk_amount": 10.0}, 2).risk_amount == 10.0
    assert StrategyPolicy.parse(policy | {"risk_mode": "fixed_lots", "fixed_lots": 0.1}, 2).fixed_lots == 0.1
    assert StrategyPolicy.parse(policy | {"risk_mode": "cash", "risk_amount": 10.0,
                                   "total_risk_pct": 0.1}, 2).total_risk_pct == 0.1
    for invalid in ({"risk_mode": "cash", "risk_amount": 0},
                    {"risk_mode": "fixed_lots", "fixed_lots": 0},
                    {"risk_mode": "percent", "risk_amount": 10},
                    {"risk_mode": "cash", "risk_amount": 10, "fixed_lots": 0.1},
                    {"risk_mode": "unknown"}):
        with pytest.raises(ValueError):
            StrategyPolicy.parse(policy | invalid, 2)


@pytest.mark.parametrize("change", [{"account": "other"}, {"server": "real"}, {"demo": False}, {"magic": 3}, {"time": 1}, {"equity": math.nan}])
def test_snapshot_rejected(snapshot, change):
    with pytest.raises(ValueError):
        MarketSnapshot.parse(snapshot | change, "12345", "TEST-Demo", 26092751, time.time())


def test_snapshot_reports_stale_time_and_zero_equity(snapshot):
    with pytest.raises(ValueError, match="快照已過期"):
        MarketSnapshot.parse(snapshot | {"time": 1}, "12345", "TEST-Demo", 26092751, time.time())
    with pytest.raises(ValueError, match="權益為零"):
        MarketSnapshot.parse(snapshot | {"equity": 0}, "12345", "TEST-Demo", 26092751, time.time())


def test_no_pyramiding_or_foreign_management(policy, snapshot, decision):
    snapshot["positions"] = [{"id": "9", "symbol": "XAUUSD", "owned": False, "side": "SELL", "sl": 3010}]
    p = StrategyPolicy.parse(policy, 1)
    with pytest.raises(ValueError):
        DecisionProposal.parse(decision, p, snapshot)
    with pytest.raises(ValueError):
        DecisionProposal.parse(decision | {"action": "CLOSE", "position_id": "9"}, p, snapshot)


def test_stop_cannot_widen(policy, snapshot, decision):
    snapshot["positions"] = [{"id": "9", "symbol": "XAUUSD", "owned": True, "side": "SELL", "sl": 3010}]
    p = StrategyPolicy.parse(policy, 1)
    with pytest.raises(ValueError):
        DecisionProposal.parse(decision | {"action": "TIGHTEN", "position_id": "9", "sl": 3011}, p, snapshot)
    assert DecisionProposal.parse(decision | {"action": "TIGHTEN", "position_id": "9", "sl": 3005}, p, snapshot)


def test_result_identity():
    with pytest.raises(ValueError):
        ExecutionResult.parse({"id": "abc", "status": "DONE", "retcode": 10009, "detail": "ok", "time": 1, "account": "wrong", "server": "TEST-Demo"}, "12345", "TEST-Demo")


def test_result_latency_is_bounded_and_older_results_remain_valid():
    result = {"id": "abc", "status": "DONE", "retcode": 10009, "detail": "ok",
              "time": 1, "account": "12345", "server": "TEST-Demo"}
    assert ExecutionResult.parse(result, "12345", "TEST-Demo").latency_ms == 0
    assert ExecutionResult.parse(result | {"latency_ms": 250}, "12345", "TEST-Demo").latency_ms == 250
    for invalid in (-1, 60001, "250", True):
        with pytest.raises((ValueError, TypeError)):
            ExecutionResult.parse(result | {"latency_ms": invalid}, "12345", "TEST-Demo")
