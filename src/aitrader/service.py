from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .bridge import Bridge
from .contracts import DecisionProposal, ExecutionResult, MarketSnapshot, StrategyPolicy, number
from .provider import ADAPTERS, build_provider
from .storage import Store, dumps
from .symbols import ambiguous_choice, catalog, relevant

REQUIRED_EA_VERSION = "1.010"


def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("bridge_dir", "database"):
        value = Path(os.path.expandvars(cfg[key]))
        cfg[key] = str(value if value.is_absolute() else (path.parent / value).resolve())
    if not str(cfg.get("account", "")).isdecimal() or not cfg.get("server"):
        raise ValueError("請先設定精確帳號與伺服器")
    cfg["account"] = str(cfg["account"])
    cfg.setdefault("account_mode", "demo")
    cfg.setdefault("live_enabled", False)
    if cfg["account_mode"] not in {"demo", "real"} or type(cfg["live_enabled"]) is not bool:
        raise ValueError("帳戶模式設定無效")
    if cfg["account_mode"] == "demo" and cfg["live_enabled"]:
        raise ValueError("模擬帳戶不能啟用實盤權限")
    if type(cfg["provider"].get("enabled")) is not bool or type(cfg["telegram"].get("enabled")) is not bool:
        raise ValueError("enabled flags must be JSON booleans")
    if any(c in cfg["server"] for c in ',\r\n"'):
        raise ValueError("unsupported server name")
    number(cfg["magic"], 1, 2**31-1)
    number(cfg["analysis_interval_seconds"], 60, 86400)
    number(cfg["command_ttl_seconds"], 10, 120)
    number(cfg["snapshot_max_age_seconds"], 1, 30)
    p = cfg["provider"]
    if p["kind"] not in ADAPTERS:
        raise ValueError("AI provider adapter not installed")
    if p.get("api_key_env") != {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}[p["kind"]]:
        raise ValueError("AI API 金鑰來源與所選供應商不一致")
    number(p["max_calls_per_day"], 1, 10000)
    number(p["min_interval_seconds"], 1, 3600)
    number(p["timeout_seconds"], 1, 60)
    number(p["max_output_tokens"], 128, 16000)
    tg = cfg["telegram"]
    if tg["enabled"] and (not isinstance(tg["user_id"], int) or tg["user_id"] <= 0 or tg["chat_id"] != tg["user_id"]):
        raise ValueError("Telegram requires explicit private user/chat pairing")
    return cfg


class Agent:
    def __init__(self, cfg, provider=None):
        self.cfg = cfg
        self.bridge = Bridge(cfg["bridge_dir"])
        self.store = Store(cfg["database"])
        identity = [cfg["account"], cfg["server"], cfg["magic"], cfg.get("account_mode", "demo")]
        prior = self.store.get("identity", identity)
        if prior not in (identity, identity[:3] if identity[3] == "demo" else identity):
            raise ValueError("database belongs to another account")
        self.store.set("identity", identity)
        self.provider = provider or build_provider(cfg["provider"], self.store)
        self.api_status = "尚未呼叫"

    def policy(self):
        data = self.store.get("policy")
        return StrategyPolicy.parse(data, data["version"]) if data else None

    def snapshot(self):
        return MarketSnapshot.parse(self.bridge.json("snapshot.json"), self.cfg["account"], self.cfg["server"], self.cfg["magic"], time.time(), self.cfg["snapshot_max_age_seconds"], self.cfg.get("account_mode", "demo")).data

    def execution_context(self):
        """Observed broker-call timing and failures for this account only."""
        by_symbol = {}
        rows = self.store.db.execute("SELECT data FROM events WHERE kind='execution' ORDER BY id DESC LIMIT 40").fetchall()
        for row in rows:
            result = json.loads(row[0])
            command = self.store.db.execute("SELECT data FROM commands WHERE id=?", (result.get("id"),)).fetchone()
            if not command:
                continue
            symbol = json.loads(command[0]).get("symbol")
            if not symbol:
                continue
            bucket = by_symbol.setdefault(symbol, {"samples": 0, "rejections": 0, "latencies": []})
            bucket["samples"] += 1
            bucket["rejections"] += result.get("status") in {"REJECTED", "UNCERTAIN"}
            latency = result.get("latency_ms", 0)
            if isinstance(latency, (int, float)) and 0 < latency <= 60000:
                bucket["latencies"].append(latency)
        context = {}
        for symbol, bucket in by_symbol.items():
            times = sorted(bucket["latencies"])
            context[symbol] = {"samples": bucket["samples"], "rejections": bucket["rejections"],
                               "median_latency_ms": times[len(times)//2] if times else None,
                               "p90_latency_ms": times[min(len(times)-1, (len(times)*9+9)//10-1)] if times else None}
        return context

    def version(self):
        p = self.policy()
        return p.version if p else 0

    def propose(self, kind, data):
        identifier = uuid.uuid4().hex
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='superseded' WHERE status='pending'")
            self.store.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'pending')", (identifier, self.version(), time.time()+600, kind, dumps(data)))
        self.store.set("pending", identifier)
        self.store.event("proposal", {"id": identifier, "kind": kind, "data": data})
        self.publish()
        return identifier

    def auto_draft(self, detail=""):
        percentages = re.findall(r"(\d+(?:\.\d+)?)\s*%", detail)
        if len(percentages) > 1:
            raise ValueError("一次請設定一種風險數值；分別說明每筆或總持倉風險")
        per_trade, total = None, None
        if percentages:
            amount = float(percentages[0])
            if "總風險" in detail or "總持倉風險" in detail or "合計風險" in detail:
                if not 0.01 <= amount <= 1.5:
                    raise ValueError("總持倉風險須在 0.01% 至 1.5% 之間")
                total = amount
            else:
                if not 0.01 <= amount <= 0.5:
                    raise ValueError("目前每筆交易風險上限是 0.5%；請改填 0.5% 以下")
                per_trade = amount
        instruction = ("請根據目前可分析商品的已完成 K 棒，自行選擇適合的交易方法與商品；"
                       "可以比較趨勢、突破、區間或結構，但須明確寫出各方法何時適用、進場確認、失效與退出條件。"
                       "把它整理成可檢閱的策略卡；後續每次分析可在已確認方法內擇優或觀望，"
                       "不得自行新增方法、改變方向或風控。" + ("使用者補充：" + detail if detail else ""))
        return self.draft(instruction, auto=True, per_trade=per_trade, total=total)

    def draft(self, instruction, auto=False, per_trade=None, total=None):
        snapshot = self.snapshot()
        if snapshot.get("ea_version") != REQUIRED_EA_VERSION:
            raise ValueError("請先在 MT5 重新掛載 v1.010 EA，再建立策略")
        current = self.policy()
        # A strategy draft needs historical bars, not a live quote or known commission.
        # The latter are mandatory only when the account is resumed and an entry is sent.
        bar_counts = {symbol: {tf: len(market.get("bars", {}).get(tf) or []) for tf in ("M15", "H1", "H4")}
                      for symbol, market in snapshot["symbols"].items() if isinstance(market, dict)
                      and isinstance(market.get("bars"), dict)} if auto else {}
        available = ([symbol for symbol, counts in bar_counts.items()
                      if counts["M15"] >= 20 and counts["H1"] >= 20]
                     if auto else catalog(self, snapshot))
        choices = available if auto else relevant(available, instruction, list(snapshot["symbols"]))
        if auto and not choices:
            details = "、".join(symbol + " M15=" + str(counts["M15"]) + " H1=" + str(counts["H1"])
                               for symbol, counts in list(bar_counts.items())[:5])
            raise ValueError("尚無商品同時具備至少 20 根 M15 和 H1 已完成 K 棒" +
                             ("（" + details + "）" if details else "（EA 尚未提供商品 K 棒）") +
                             "；請先在 MT5 市場報價顯示商品、開啟歷史行情後重試")
        if not choices:
            raise ValueError("MT5 市場報價沒有可分析的商品；請先手動顯示要交易的商品，等待 EA 更新行情")
        request = {"request": instruction, "current_policy": current.to_dict() if current else None,
                   "available_symbols": choices, "missing_symbols": snapshot.get("missing_symbols", [])}
        if auto:
            request["auto_mode"] = True
            request["bar_fields"] = ["time", "open", "high", "low", "close", "volume"]
            bar_limits = {"M15": 20, "H1": 20, "H4": 12}
            request["market_context"] = {symbol: {
                "ready": market.get("ready", False), "bid": market.get("bid"), "ask": market.get("ask"),
                "spread_points": market.get("spread_points"), "commission_round_turn": market.get("commission_round_turn"),
                "bars": {tf: [[bar.get(field) for field in request["bar_fields"]]
                              for bar in (market.get("bars", {}).get(tf) or [])[-limit:] if isinstance(bar, dict)]
                         for tf, limit in bar_limits.items()}}
                for symbol, market in snapshot["symbols"].items() if symbol in choices}
        result = self.provider.call("strategy", request)
        if result.get("questions") or not result.get("policy"):
            return "需要釐清：" + dumps(result.get("questions", ["策略資料不足"]))
        if not isinstance(result["policy"], dict):
            raise ValueError("AI 策略卡格式不正確")
        proposed = result["policy"]
        if per_trade is not None:
            proposed = proposed | {"risk_pct": per_trade}
        if total is not None:
            proposed = proposed | {"total_risk_pct": total}
        policy = StrategyPolicy.parse(proposed, self.version()+1)
        if len(dumps(policy.to_dict())) > 10000:
            raise ValueError("策略卡過長，請縮短為可完整檢閱的策略")
        if set(policy.symbols) - set(choices):
            raise ValueError("策略包含不在券商商品清單內的商品")
        for symbol in policy.symbols:
            candidates = ambiguous_choice(symbol, instruction, available)
            if candidates:
                return "需要選擇 " + symbol + " 的券商商品後綴：" + "、".join(candidates) + "。請在策略中寫出完整商品代碼。"
        # Explicit direction phrases are enforced independently of model interpretation.
        if any(x in instruction.lower() for x in ("只做空", "只放空", "only sell", "short only")) and policy.direction != "SELL":
            raise ValueError("AI 未遵守只做空要求，草案被拒絕")
        if any(x in instruction.lower() for x in ("只做多", "only buy", "long only")) and policy.direction != "BUY":
            raise ValueError("AI 未遵守只做多要求，草案被拒絕")
        identifier = self.propose("policy", policy.to_dict())
        before = current.to_dict() if current else {}
        changes = {k: {"原": before.get(k), "新": v} for k, v in policy.to_dict().items() if before.get(k) != v}
        # Full new card plus concise change names avoids truncating old/new long text.
        return "策略草案（確認只保存，啟動需另行確認）：\n" + dumps(policy.to_dict()) + "\n變更欄位：" + ", ".join(changes) + "\n確認 " + identifier

    def confirm(self, identifier):
        row = self.store.db.execute("SELECT * FROM proposals WHERE id=?", (identifier,)).fetchone()
        if not row or row["status"] != "pending" or row["expires"] < time.time() or row["base_version"] != self.version():
            raise ValueError("提案已過期、已使用或策略版本已變更")
        data = json.loads(row["data"])
        # Consume before side effects. A crash may require another proposal, never duplicate actions.
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='confirmed' WHERE id=? AND status='pending'", (identifier,))
        self.store.set("pending", "")
        if row["kind"] == "policy":
            p = StrategyPolicy.parse(data, self.version()+1)
            snapshot = self.snapshot()
            if any(x["owned"] and x["symbol"] not in p.symbols for x in snapshot["positions"]):
                raise ValueError("不可從策略移除仍有本系統持倉的商品，請先平倉")
            self.store.set("paused", True)
            self.store.set("policy", p.to_dict())
        elif row["kind"] == "resume":
            snapshot = self.snapshot()
            if snapshot.get("ea_version") != REQUIRED_EA_VERSION:
                raise ValueError("請先在 MT5 重新掛載 v1.010 EA，再啟動新單")
            p = self.policy()
            if self.cfg.get("account_mode", "demo") == "real" and not self.cfg.get("live_enabled", False):
                raise ValueError("此實盤帳號尚未授權自動新單")
            if not p or snapshot.get("halted") or snapshot.get("state_ok") is not True:
                raise ValueError("尚無策略或 EA 風控鎖定，不能啟動")
            if set(p.symbols) - set(snapshot["symbols"]) or not all(snapshot["symbols"][s].get("ready") for s in p.symbols):
                raise ValueError("行情尚未就緒")
            self.store.set("paused", False)
            self.store.set("resume_nonce", self.store.get("resume_nonce", 0)+1)
        elif row["kind"] == "close":
            snapshot = self.snapshot()
            self.store.set("paused", True)
            for pos in snapshot["positions"]:
                if pos["owned"] and pos["id"] in data["ids"]:
                    self.queue(DecisionProposal("CLOSE", pos["symbol"], "使用者確認平倉", position_id=str(pos["id"])), "manual", time.time())
        elif row["kind"] == "reset":
            snapshot = self.snapshot()
            if any(p["owned"] for p in snapshot["positions"]):
                raise ValueError("有本系統持倉，不能重設總回撤")
            self.store.set("paused", True)
            self.store.set("reset_nonce", self.store.get("reset_nonce", 0)+1)
        self.store.event("confirmed", {"id": identifier, "kind": row["kind"]})
        self.publish()
        return "已套用。" + ("目前暫停新單。" if self.store.get("paused", True) else ("實盤" if self.cfg.get("account_mode", "demo") == "real" else "模擬") + "自動交易已啟用。")

    def handle(self, message):
        message = message.strip()
        if message in {"/start", "/help", "說明"}:
            return "自動模式：AI 依目前商品行情提出交易方法。\n策略 用SMC只做空…\n狀態｜持倉｜原因｜暫停｜啟動｜平倉｜重設回撤\n確認 <提案ID>\n其他問題為只讀 AI 查詢。策略保存後需確認啟動。"
        if message in {"自動模式", "/auto"} or message.startswith(("自動模式 ", "/auto ")):
            detail = message.split(" ", 1)[1].strip() if " " in message else ""
            return self.auto_draft(detail)
        if re.fullmatch(r"(?:(?:每筆|單筆)(?:交易)?\s*)?(?:風險\s*)?\d+(?:\.\d+)?\s*%\s*(?:風險)?", message):
            return self.auto_draft(message)
        if message.startswith(("策略 ", "/strategy ")):
            return self.draft(message.split(" ", 1)[1])
        if message.startswith(("確認 ", "/confirm ")):
            return self.confirm(message.split(" ", 1)[1].strip())
        if message in {"暫停", "/pause"}:
            self.store.set("paused", True)
            self.publish()
            return "已暫停新單，既有持倉保護繼續。"
        if message in {"啟動", "/resume"}:
            if not self.policy():
                return "請先建立並確認策略。"
            if self.cfg.get("account_mode", "demo") == "real" and not self.cfg.get("live_enabled", False):
                return "此實盤帳號尚未在設定視窗授權自動新單。"
            return "啟用" + ("實盤" if self.cfg.get("account_mode", "demo") == "real" else "模擬") + "自動交易，確認 " + self.propose("resume", {})
        if message in {"平倉", "/close"}:
            ids = [p["id"] for p in self.snapshot()["positions"] if p["owned"]]
            return "暫停新單並平掉本系統目前持倉 " + dumps(ids) + "；確認 " + self.propose("close", {"ids": ids})
        if message in {"重設回撤", "/reset"}:
            return "將重設总回撤基準並保持暫停；確認 " + self.propose("reset", {})
        if message in {"狀態", "持倉", "/status", "/positions"}:
            s = self.snapshot()
            p = self.policy()
            mode_label = "實盤" if self.cfg.get("account_mode", "demo") == "real" else "模擬"
            parts = ["【" + mode_label + "帳號】目前" + ("暫停新單" if self.store.get("paused", True) else "允許自動交易") + "。",
                     "權益：" + f'{s["equity"]:,.2f}' + (" " + s["currency"] if s.get("currency") else "") + "。",
                     "策略：" + (p.title if p else "尚未設定；可以傳『策略 你的交易規則』建立草案") + "。"]
            positions = s["positions"]
            if positions:
                parts.append("目前持倉 " + str(len(positions)) + " 筆：" + "、".join(
                    x["symbol"] + " " + ("買入" if x["side"] == "BUY" else "賣出") +
                    ("（本系統）" if x["owned"] else "（其他）") for x in positions[:8]) + ("等。" if len(positions) > 8 else "。"))
            else:
                parts.append("目前沒有持倉。")
            if s["halted"] or s.get("state_ok") is not True:
                parts.append("風控已鎖定，暫不開新單。")
            parts.append("AI：" + self.api_status + "。")
            return "\n".join(parts)
        if message in {"原因", "/why"}:
            return dumps(self.store.memory(8))
        answer = self.provider.call("chat", {"question": message, "snapshot": self.snapshot(), "policy": self.store.get("policy"), "memory": self.store.memory()})
        reply = str(answer.get("answer", "沒有可用回答"))[:10000]
        self.store.event("conversation", {"question": message, "answer": reply})
        return reply

    def queue(self, decision, parent, now, expires=None):
        identifier = uuid.uuid4().hex
        with self.store.db:
            self.store.db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?, 'queued',NULL)", (identifier, parent, now, expires or now+self.cfg["command_ttl_seconds"], self.version(), dumps(decision.to_dict())))
        return identifier

    def ingest(self):
        results, offset = self.bridge.tail("results.jsonl", self.store.get("results_offset", 0))
        for raw in results:
            result = ExecutionResult.parse(raw, self.cfg["account"], self.cfg["server"])
            with self.store.db:
                if not self.store.db.execute("INSERT OR IGNORE INTO seen VALUES('result',?)", (result.id,)).rowcount:
                    continue
                self.store.db.execute("UPDATE commands SET status=?,result=? WHERE id=?", (result.status, dumps(raw), result.id))
                self.store.db.execute("INSERT INTO events(time,kind,data) VALUES(?,'execution',?)", (time.time(), dumps(raw)))
                if result.status == "UNCERTAIN":
                    self.store.db.execute("INSERT INTO state VALUES('paused','true') ON CONFLICT(key) DO UPDATE SET value='true'")
        self.store.set("results_offset", offset)
        deals, offset = self.bridge.tail("deals.jsonl", self.store.get("deals_offset", 0))
        for deal in deals:
            if deal.get("account") != self.cfg["account"] or deal.get("server") != self.cfg["server"]:
                raise ValueError("deal identity mismatch")
            with self.store.db:
                if self.store.db.execute("INSERT OR IGNORE INTO seen VALUES('deal',?)", (str(deal["deal"]),)).rowcount:
                    self.store.db.execute("INSERT INTO events(time,kind,data) VALUES(?,'deal',?)", (time.time(), dumps(deal)))
        self.store.set("deals_offset", offset)

    def dispatch(self):
        now = time.time()
        pending = self.store.db.execute("SELECT * FROM commands WHERE status='sent' ORDER BY created LIMIT 1").fetchone()
        if pending:
            if pending["expires"] < now:
                with self.store.db:
                    self.store.db.execute("UPDATE commands SET status='UNCERTAIN' WHERE id=?", (pending["id"],))
                self.store.set("paused", True)
                self.store.event("uncertain", {"id": pending["id"], "reason": "回報逾時，暫停新單，需查看帳戶後再啟動"})
            return
        row = self.store.db.execute("SELECT * FROM commands WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if not row:
            return
        d = json.loads(row["data"])
        if row["expires"] < now or row["version"] != self.version() or (self.store.get("paused", True) and d["action"] in {"BUY", "SELL", "REVERSE"}):
            with self.store.db:
                self.store.db.execute("UPDATE commands SET status='cancelled' WHERE id=?", (row["id"],))
            return
        snapshot = self.snapshot()
        if snapshot.get("ea_version") != REQUIRED_EA_VERSION and d["action"] in {"BUY", "SELL", "REVERSE"}:
            with self.store.db:
                self.store.db.execute("UPDATE commands SET status='cancelled' WHERE id=?", (row["id"],))
            self.store.event("decision_blocked", {"reason": "EA version mismatch", "id": row["id"]})
            return
        p = self.policy()
        if not p:
            return
        try:
            decision = DecisionProposal.parse(d, p, snapshot)
        except ValueError:
            with self.store.db:
                self.store.db.execute("UPDATE commands SET status='REJECTED' WHERE id=?", (row["id"],))
            return
        action = "CLOSE" if decision.action == "REVERSE" else decision.action
        # Commit before file publication: never resend after an ambiguous crash.
        with self.store.db:
            self.store.db.execute("UPDATE commands SET status='sent' WHERE id=?", (row["id"],))
        self.bridge.csv("command.csv", [1, row["id"], self.cfg["account"], self.cfg["server"], self.cfg["magic"], row["version"], int(row["expires"]), action, d["symbol"], d["position_id"], d["sl"], d["tp"]])

    def reverse_followups(self):
        rows = self.store.db.execute("SELECT * FROM commands WHERE status='DONE'").fetchall()
        for row in rows:
            d = json.loads(row["data"])
            if d["action"] != "REVERSE" or self.store.get("paused", True):
                continue
            if row["expires"] < time.time() or row["version"] != self.version():
                continue
            snapshot = self.snapshot()
            result = json.loads(row["result"])
            if snapshot["time"] <= result["time"] or any(p["symbol"] == d["symbol"] for p in snapshot["positions"]):
                continue
            if self.store.seen("reverse", row["id"]):
                continue
            d |= {"action": d["reverse_to"], "position_id": "0", "reverse_to": ""}
            try:
                decision = DecisionProposal.parse(d, self.policy(), snapshot)
                self.queue(decision, row["id"], time.time(), row["expires"])
            except ValueError:
                self.store.event("reverse_cancelled", {"id": row["id"], "reason": "反向價格或風控條件已失效"})

    def analyze(self):
        p = self.policy()
        if not p:
            return
        if self.store.db.execute("SELECT 1 FROM commands WHERE status IN ('queued','sent')").fetchone():
            return
        snapshot = self.snapshot()
        if snapshot.get("ea_version") != REQUIRED_EA_VERSION:
            return
        paused = self.store.get("paused", True)
        if paused and not any(x["owned"] for x in snapshot["positions"]):
            return
        if set(p.symbols) - set(snapshot["symbols"]):
            return  # Market Watch removal must not trigger an AI decision or new order.
        if snapshot.get("halted") or snapshot.get("state_ok") is not True:
            return
        now = time.time()
        signature = [[x["id"], x["volume"], x["sl"]] for x in snapshot["positions"] if x["owned"]]
        changed = signature != self.store.get("position_signature", [])
        interval = 60 if changed else self.cfg["analysis_interval_seconds"]
        if now - self.store.get("last_analysis", 0) < interval:
            return
        self.store.set("last_analysis", now)
        self.store.set("position_signature", signature)
        execution_context = self.execution_context()
        self.store.event("analysis_input", {"snapshot": snapshot, "policy": p.to_dict(), "model": self.cfg["provider"]["model"],
                                            "execution_context": execution_context})
        try:
            response = self.provider.call("decisions", {"policy": p.to_dict(), "snapshot": snapshot,
                                                        "execution_context": execution_context,
                                                        "new_entries_paused": paused, "memory": self.store.memory()})
        except ValueError as exc:
            if str(exc) == "API local cooldown active":
                # Multi-account agents share one key. Retry when the shared call interval ends.
                delay = self.cfg["provider"]["min_interval_seconds"] + 1
                self.store.set("last_analysis", now - interval + delay)
            raise
        fresh = self.snapshot()  # Provider latency never extends validity of old market observations.
        if time.time() - snapshot["time"] > self.cfg["command_ttl_seconds"] or p.version != self.version():
            raise ValueError("analysis expired")
        raw = response.get("decisions")
        if not isinstance(raw, list) or len(raw) > len(p.symbols):
            raise ValueError("invalid decisions list")
        decisions = [DecisionProposal.parse(x, p, fresh) for x in raw]
        if len({d.symbol for d in decisions}) != len(decisions):
            raise ValueError("multiple decisions for one symbol")
        for d in decisions:
            if paused and d.action in {"BUY", "SELL", "REVERSE"}:
                self.store.event("decision_blocked", {"reason": "new entries paused", "decision": d.to_dict()})
                continue
            self.store.event("decision", {"policy_version": p.version, "snapshot_time": snapshot["time"], "model": self.cfg["provider"]["model"], "decision": d.to_dict()})
            if d.action not in {"WAIT", "HOLD"}:
                self.queue(d, "ai", time.time(), snapshot["time"]+self.cfg["command_ttl_seconds"])
        self.api_status = "分析完成"

    def publish(self):
        p = self.policy()
        paused = self.store.get("paused", True)
        if p:
            try:
                ea_ready = self.snapshot().get("ea_version") == REQUIRED_EA_VERSION
            except (ValueError, FileNotFoundError, OSError):
                ea_ready = False
            entries_allowed = ea_ready and not paused and (self.cfg.get("account_mode", "demo") == "demo" or self.cfg.get("live_enabled", False))
            self.bridge.csv("policy.csv", [1, self.cfg["account"], self.cfg["server"], self.cfg["magic"], p.version, int(time.time())+45,
                       int(entries_allowed), p.direction, p.risk_pct, p.total_risk_pct, p.daily_loss_pct, p.drawdown_pct,
                       "|".join(p.symbols), self.store.get("reset_nonce", 0), self.store.get("resume_nonce", 0)])
        pending = self.store.get("pending", "")
        row = self.store.db.execute("SELECT * FROM proposals WHERE id=? AND status='pending' AND expires>?", (pending, time.time())).fetchone()
        pending_text = (row["kind"]+" "+row["id"]+"\n"+row["data"]) if row else ""
        self.bridge.csv("pending.csv", [row["id"] if row else "none"])
        events = self.store.db.execute("SELECT data FROM events WHERE kind IN ('decision','execution','error') ORDER BY id DESC LIMIT 1").fetchone()
        replies = self.store.db.execute("SELECT data FROM events WHERE kind='panel_reply' ORDER BY id DESC LIMIT 3").fetchall()
        chat = "\n".join("你：" + str(item.get("question", ""))[:1000] + "\n回覆：" + str(item.get("answer", ""))[:10000]
                         for item in (json.loads(row[0]) for row in reversed(replies)))
        mode_label = "實盤" if self.cfg.get("account_mode", "demo") == "real" else "模擬"
        self.bridge.status({"time": int(time.time()), "headline": mode_label + " | " + ("暫停新單" if paused else "自動交易"),
                           "strategy": p.title if p else "尚無已確認策略", "api": self.api_status,
                           "chat": chat, "latest": events[0][:300] if events else "", "pending": pending_text})

    def panel_events(self):
        events, offset = self.bridge.tail("ui.jsonl", self.store.get("ui_offset", 0))
        for event in events:
            if event.get("account") != self.cfg["account"] or event.get("server") != self.cfg["server"]:
                continue
            if self.store.seen("ui", event["id"]):
                continue
            if abs(time.time()-event["time"]) > 60:
                continue
            action = event.get("action")
            command = {"pause": "暫停", "resume": "啟動", "close": "平倉", "sync": "狀態", "reset": "重設回撤"}.get(action)
            if action == "confirm":
                command = "確認 " + event.get("proposal", "")
            if action == "chat":
                candidate = event.get("text")
                if event.get("magic") != self.cfg["magic"] or not isinstance(candidate, str) or not 1 <= len(candidate.strip()) <= 1000 or any(ord(c) < 32 for c in candidate):
                    continue
                command = candidate.strip()
            if command:
                try:
                    reply = self.handle(command)
                except Exception as exc:
                    reply = "未套用：" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
                self.store.event("panel_reply", {"question": command, "answer": reply})
        self.store.set("ui_offset", offset)

    def tick(self):
        self.ingest()
        self.panel_events()
        self.publish()
        self.reverse_followups()
        self.dispatch()
        self.analyze()

    def close(self):
        self.store.db.close()
