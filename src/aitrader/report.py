import json
import time
from collections import defaultdict

from .contracts import DecisionProposal, StrategyPolicy


def report(store):
    grouped = defaultdict(lambda: {"net": 0, "entry_volume": 0, "exit_volume": 0})
    for row in store.db.execute("SELECT data FROM events WHERE kind='deal'"):
        d = json.loads(row[0])
        p = grouped[d["position_id"]]
        p["net"] += sum(d.get(k, 0) for k in ("profit", "commission", "swap", "fee"))
        if d["entry"] == 0:
            p["entry_volume"] += d["volume"]
        elif d["entry"] in {1, 3}:
            p["exit_volume"] += d["volume"]
    closed = [p for p in grouped.values() if p["entry_volume"]>0 and p["exit_volume"]>=p["entry_volume"]-1e-8]
    first = store.get("forward_started")
    days = (time.time()-first)/86400 if first else 0
    good = sum(max(0, p["net"]) for p in closed)
    bad = -sum(min(0, p["net"]) for p in closed)
    equity = []
    for row in store.db.execute("SELECT data FROM events WHERE kind='equity' ORDER BY id"):
        equity.append(json.loads(row[0])["equity"])
    peak, dd = 0, 0
    for value in equity:
        peak = max(peak, value)
        if peak:
            dd = max(dd, (peak-value)/peak*100)
    calls = [dict(r) for r in store.db.execute("SELECT status,COUNT(*) AS count,SUM(latency) AS latency,SUM(cost) AS cost FROM calls GROUP BY status")]
    tokens = sum(json.loads(r[0]).get("totalTokenCount", 0) for r in store.db.execute("SELECT usage FROM calls WHERE usage IS NOT NULL"))
    return {"demo_only": True, "elapsed_days": round(days, 2), "closed_positions": len(closed),
            "net_closed_profit": round(sum(p["net"] for p in closed), 2), "profit_factor": good/bad if bad else None,
            "sampled_equity_drawdown_pct": dd, "equity_sample_count": len(equity),
            "observation_minimum_met": days>=28 and len(closed)>=100, "live_approved": False,
            "api_calls": calls, "api_total_tokens": tokens, "api_cost_note": "null means unavailable; Gemini API does not report actual billing cost",
            "limitations": "Sampled drawdown can miss intrasecond lows; elapsed days do not prove uninterrupted observation. Broker statement reconciliation required."}


def replay(store):
    """Offline contract replay of actually recorded inputs and decisions; never calls AI."""
    snapshot = policy = None
    accepted = rejected = 0
    for row in store.db.execute("SELECT kind,data FROM events WHERE kind IN ('analysis_input','decision') ORDER BY id"):
        data = json.loads(row["data"])
        if row["kind"] == "analysis_input":
            snapshot = data["snapshot"]
            policy = StrategyPolicy.parse(data["policy"], data["policy"]["version"])
        elif snapshot is not None:
            try:
                DecisionProposal.parse(data["decision"], policy, snapshot)
                accepted += 1
            except (ValueError, TypeError, KeyError):
                rejected += 1
    return {"validated_decisions": accepted, "rejected": rejected,
            "scope": "recorded contract replay only; not a fill simulator or historical profitability backtest"}
