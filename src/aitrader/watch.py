"""Bounded, locally monitored entry setups proposed by the model."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .contracts import DecisionProposal, StrategyPolicy, number, text


@dataclass(frozen=True)
class WatchCandidate:
    symbol: str
    basis: str
    timeframe: str
    trigger_operator: str
    trigger_price: float
    invalidation_operator: str
    invalidation_price: float
    expires: int
    reason: str
    decision: dict
    policy_version: int
    reference_bar_time: int

    @classmethod
    def parse(cls, raw: dict, policy: StrategyPolicy, snapshot: dict, now: int) -> WatchCandidate:
        required = {"symbol", "basis", "timeframe", "trigger_operator", "trigger_price",
                    "invalidation_operator", "invalidation_price", "expires", "reason", "decision"}
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("invalid watch fields")
        symbol = raw["symbol"]
        if symbol not in policy.symbols or symbol not in snapshot["symbols"]:
            raise ValueError("watch symbol outside policy")
        if raw["basis"] not in {"QUOTE", "CLOSE"} or raw["timeframe"] not in policy.timeframes:
            raise ValueError("invalid watch basis or timeframe")
        if raw["trigger_operator"] not in {"ABOVE", "BELOW"} or raw["invalidation_operator"] not in {"ABOVE", "BELOW"}:
            raise ValueError("invalid watch comparison")
        trigger = number(raw["trigger_price"], 0.00000001, 1e9)
        invalidation = number(raw["invalidation_price"], 0.00000001, 1e9)
        expiry = number(raw["expires"], now + 60, now + 86400)
        reason = text(raw["reason"], 1000)
        decision = DecisionProposal.parse(raw["decision"], policy, snapshot)
        if decision.action not in {"BUY", "SELL"} or decision.symbol != symbol:
            raise ValueError("watch requires an entry for the same symbol")
        if (decision.action == "BUY" and invalidation >= snapshot["symbols"][symbol]["bid"] and
            raw["invalidation_operator"] == "BELOW") or (
            decision.action == "SELL" and invalidation <= snapshot["symbols"][symbol]["ask"] and
            raw["invalidation_operator"] == "ABOVE"):
            raise ValueError("watch already invalidated")
        bars = snapshot["symbols"][symbol].get("bars", {}).get(raw["timeframe"], [])
        reference = int(bars[-1]["time"]) if raw["basis"] == "CLOSE" and bars else 0
        if raw["basis"] == "CLOSE" and reference <= 0:
            raise ValueError("watch needs a completed reference bar")
        if raw["basis"] == "CLOSE" and snapshot["symbols"][symbol].get("ready") is True:
            latest_close = bars[-1]["close"]
            if (cls.crossed(latest_close, raw["trigger_operator"], trigger) or
                cls.crossed(latest_close, raw["invalidation_operator"], invalidation)):
                raise ValueError("watch trigger or invalidation already reached")
        watch = cls(symbol, raw["basis"], raw["timeframe"], raw["trigger_operator"], trigger,
                    raw["invalidation_operator"], invalidation, int(expiry), reason,
                    decision.to_dict(), policy.version, reference)
        if watch.check(snapshot, now) != "waiting":
            raise ValueError("watch trigger or invalidation already reached")
        return watch

    @staticmethod
    def crossed(value: float, operator: str, threshold: float) -> bool:
        return value >= threshold if operator == "ABOVE" else value <= threshold

    def check(self, snapshot: dict, now: int) -> str:
        if now >= self.expires:
            return "expired"
        market = snapshot["symbols"].get(self.symbol)
        if not isinstance(market, dict) or market.get("ready") is not True:
            return "waiting"
        if self.basis == "QUOTE":
            invalidation_value = market["ask"] if self.invalidation_operator == "ABOVE" else market["bid"]
            trigger_value = market["ask"] if self.trigger_operator == "ABOVE" else market["bid"]
        else:
            bars = market.get("bars", {}).get(self.timeframe) or []
            if not bars or int(bars[-1]["time"]) <= self.reference_bar_time:
                return "waiting"
            invalidation_value = trigger_value = bars[-1]["close"]
        if self.crossed(invalidation_value, self.invalidation_operator, self.invalidation_price):
            return "invalidated"
        if self.crossed(trigger_value, self.trigger_operator, self.trigger_price):
            return "triggered"
        return "waiting"

    def to_dict(self) -> dict:
        return asdict(self)
