import copy
import json
import time

import pytest

from aitrader.bridge import atomic_write
from aitrader.service import Agent


@pytest.fixture
def policy():
    return {"version": 1, "title": "SMC 日內", "instructions": "只做空，依結構分析", "direction": "SELL",
            "symbols": ["XAUUSD", "EURUSD"], "timeframes": ["M5", "M15", "H1", "H4"],
            "entry": "確認結構破位與回測", "invalidation": "突破前高", "management": "失效提前出場",
            "definitions": "BOS：收棒跌破已確認 swing low；FVG：三根已收棒不重疊區間。",
            "risk_pct": .5, "total_risk_pct": 1.5, "daily_loss_pct": 2, "drawdown_pct": 5}


@pytest.fixture
def snapshot():
    return {"schema": 1, "account": "12345", "server": "TEST-Demo", "magic": 26092751, "demo": True,
            "time": int(time.time()), "equity": 10000, "balance": 10000, "state_ok": True, "halted": False,
            "positions": [], "symbols": {"XAUUSD": {"ready": True, "bid": 3000, "ask": 3000.2},
                                        "EURUSD": {"ready": True, "bid": 1.1, "ask": 1.1001}}}


@pytest.fixture
def decision():
    return {"action": "SELL", "symbol": "XAUUSD", "reason": "跌破結構", "invalidation": "回到前高", "management": "收緊停損",
            "sl": 3010, "tp": 2980, "position_id": "0", "reverse_to": ""}


class FakeProvider:
    def __init__(self):
        self.response = {}
        self.calls = []
        self.error = None

    def call(self, kind, payload):
        self.calls.append((kind, copy.deepcopy(payload)))
        if self.error:
            raise self.error
        return copy.deepcopy(self.response)


@pytest.fixture
def agent(tmp_path, snapshot, policy):
    cfg = {"bridge_dir": str(tmp_path / "bridge"), "database": str(tmp_path / "agent.sqlite"),
           "account": "12345", "server": "TEST-Demo", "magic": 26092751,
           "provider": {"kind": "gemini", "enabled": False, "model": "test-model"},
           "snapshot_max_age_seconds": 15, "analysis_interval_seconds": 300, "command_ttl_seconds": 60}
    fake = FakeProvider()
    obj = Agent(cfg, fake)
    obj.store.set("policy", policy)
    atomic_write(obj.bridge.root / "snapshot.json", json.dumps(snapshot))
    yield obj
    obj.close()
