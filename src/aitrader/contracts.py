from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any


def number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("numeric field required")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("numeric field outside bounds")
    return float(value)


def text(value: Any, limit=8000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("invalid text field")
    return value.strip()


def token(value: Any) -> str:
    value = text(value, 100)
    if not re.fullmatch(r"[A-Za-z0-9_.#-]+", value):
        raise ValueError("invalid wire token")
    return value


@dataclass(frozen=True)
class StrategyPolicy:
    version: int
    title: str
    instructions: str
    direction: str = "BOTH"
    symbols: list[str] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=lambda: ["M5", "M15", "H1", "H4"])
    entry: str = ""
    invalidation: str = ""
    management: str = ""
    definitions: str = ""
    risk_pct: float = 0.5
    total_risk_pct: float = 1.5
    daily_loss_pct: float = 2.0
    drawdown_pct: float = 5.0

    @classmethod
    def parse(cls, data: dict, version: int) -> StrategyPolicy:
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("unknown strategy fields")
        obj = cls(**(data | {"version": version}))
        for name in ("title", "instructions", "entry", "invalidation", "management", "definitions"):
            text(getattr(obj, name))
        if obj.direction not in {"BUY", "SELL", "BOTH"}:
            raise ValueError("invalid direction")
        if not isinstance(obj.symbols, list) or not 1 <= len(obj.symbols) <= 10:
            raise ValueError("invalid symbols")
        if len(set(obj.symbols)) != len(obj.symbols):
            raise ValueError("duplicate symbols")
        for symbol in obj.symbols:
            token(symbol)
        if not obj.timeframes or set(obj.timeframes) - {"M5", "M15", "H1", "H4"}:
            raise ValueError("unsupported timeframe")
        number(obj.risk_pct, 0.01, 0.5)
        number(obj.total_risk_pct, obj.risk_pct, 1.5)
        number(obj.daily_loss_pct, 0.01, 2)
        number(obj.drawdown_pct, 0.01, 5)
        return obj

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MarketSnapshot:
    data: dict

    @classmethod
    def parse(cls, data: dict, account: str, server: str, magic: int, now: float, max_age=15, expected_mode="demo"):
        if data.get("schema") != 1 or str(data.get("account")) != account or data.get("server") != server:
            raise ValueError("snapshot identity mismatch")
        if data.get("magic") != magic or data.get("account_mode", "demo") != expected_mode:
            raise ValueError("帳戶模式或 Magic Number 不符")
        if data.get("demo") is not (expected_mode == "demo"):
            raise ValueError("MT5 帳戶類型不符")
        snapshot_time = number(data["time"], 0, 1e12)
        if snapshot_time < now - max_age:
            raise ValueError("MT5 快照已過期；確認目前 MT5 登入的是此帳號，且 EA 仍在執行")
        if snapshot_time > now + 3:
            raise ValueError("MT5 快照時間超前；請核對 Windows 時鐘")
        equity = number(data["equity"], 0, 1e12)
        if equity < 0.01:
            raise ValueError("帳戶權益為零；請檢查帳戶餘額與 MT5 登入")
        if not isinstance(data["positions"], list) or not isinstance(data["symbols"], dict):
            raise ValueError("invalid snapshot")
        return cls(data)


@dataclass(frozen=True)
class DecisionProposal:
    action: str
    symbol: str
    reason: str
    invalidation: str = ""
    management: str = ""
    sl: float = 0
    tp: float = 0
    position_id: str = "0"
    reverse_to: str = ""

    @classmethod
    def parse(cls, data: dict, policy: StrategyPolicy, snapshot: dict):
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("unknown decision fields")
        obj = cls(**data)
        if obj.action not in {"WAIT", "HOLD", "BUY", "SELL", "CLOSE", "TIGHTEN", "REVERSE"}:
            raise ValueError("invalid action")
        text(obj.reason, 2000)
        if obj.symbol not in snapshot["symbols"] or (obj.action in {"BUY", "SELL", "REVERSE"} and obj.symbol not in policy.symbols):
            raise ValueError("symbol outside policy")
        number(obj.sl, 0, 1e9)
        number(obj.tp, 0, 1e9)
        if not isinstance(obj.position_id, str) or not obj.position_id.isdecimal():
            raise ValueError("position identifier must be a decimal string")
        target = obj.reverse_to if obj.action == "REVERSE" else obj.action
        if obj.action == "REVERSE" and target not in {"BUY", "SELL"}:
            raise ValueError("invalid reversal direction")
        if target in {"BUY", "SELL"}:
            if policy.direction not in {"BOTH", target}:
                raise ValueError("direction forbidden")
            if obj.sl <= 0 or obj.tp <= 0:
                raise ValueError("entry requires SL and TP")
            text(obj.invalidation, 2000)
            text(obj.management, 2000)
            q = snapshot["symbols"][obj.symbol]
            if q.get("ready") is not True:
                raise ValueError("market data incomplete")
            if target == "BUY" and not obj.sl < q["bid"] < q["ask"] < obj.tp:
                raise ValueError("invalid buy price geometry")
            if target == "SELL" and not obj.tp < q["bid"] < q["ask"] < obj.sl:
                raise ValueError("invalid sell price geometry")
        owned = [p for p in snapshot["positions"] if p["symbol"] == obj.symbol]
        if obj.action in {"BUY", "SELL"} and owned:
            raise ValueError("symbol already occupied")
        if obj.action in {"CLOSE", "TIGHTEN", "REVERSE"}:
            p = next((p for p in owned if str(p["id"]) == obj.position_id and p["owned"]), None)
            if p is None:
                raise ValueError("position not owned")
            if obj.action == "REVERSE" and target == p["side"]:
                raise ValueError("reversal must change direction")
            if obj.action == "TIGHTEN":
                if obj.sl <= 0 or (p["side"] == "BUY" and obj.sl <= p["sl"]) or (p["side"] == "SELL" and obj.sl >= p["sl"]):
                    raise ValueError("stop must tighten")
        return obj

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ExecutionResult:
    id: str
    status: str
    retcode: int
    detail: str
    time: int
    account: str
    server: str
    latency_ms: int = 0

    @classmethod
    def parse(cls, data: dict, account: str, server: str):
        obj = cls(**data)
        if obj.account != account or obj.server != server:
            raise ValueError("execution identity mismatch")
        token(obj.id)
        if obj.status not in {"DONE", "PARTIAL", "REJECTED", "UNCERTAIN"}:
            raise ValueError("invalid execution status")
        number(obj.latency_ms, 0, 60000)
        return obj
