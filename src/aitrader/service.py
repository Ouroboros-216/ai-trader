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
from .watch import WatchCandidate

REQUIRED_EA_VERSION = "1.013"


def load_config(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if cfg.get("watch_schedule_version", 0) < 1 and cfg.get("analysis_interval_seconds") == 300:
        cfg["analysis_interval_seconds"] = 900  # Migrate the former default without editing user files.
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
    number(p["min_interval_seconds"], 1, 3600)
    number(p["timeout_seconds"], 1, 60)
    if p["kind"] == "openai":
        number(p.get("strategy_timeout_seconds", 120), 30, 300)
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

    @staticmethod
    def ai_snapshot(snapshot, policy):
        """Only strategy symbols and bounded completed bars cross the AI API."""
        fields = ("time", "open", "high", "low", "close", "volume")
        limits = {"M5": 20, "M15": 24, "H1": 16, "H4": 10}
        symbols = {}
        for symbol in policy.symbols:
            market = snapshot["symbols"].get(symbol)
            if not isinstance(market, dict):
                continue
            bars = market.get("bars", {})
            compact = {tf: [[bar.get(field) for field in fields] for bar in (bars.get(tf) or [])[-limit:]
                            if isinstance(bar, dict)] for tf, limit in limits.items() if tf in policy.timeframes}
            symbols[symbol] = {key: value for key, value in market.items() if key != "bars"} | {"bars": compact}
        return snapshot | {"symbols": symbols, "bar_fields": list(fields)}

    def clear_watches(self):
        self.store.set("watch_candidates", {})

    def monitor_watches(self):
        watched = self.store.get("watch_candidates", {})
        if not watched:
            return
        policy = self.policy()
        if not policy or self.store.get("paused", True):
            self.clear_watches()
            return
        snapshot = self.snapshot()
        now = int(time.time())
        for symbol, raw in list(watched.items()):
            try:
                candidate = WatchCandidate(**raw)
                if candidate.symbol != symbol or candidate.policy_version != policy.version:
                    state = "invalidated"
                elif any(p["symbol"] == symbol for p in snapshot["positions"]):
                    state = "invalidated"
                else:
                    state = candidate.check(snapshot, now)
            except (KeyError, TypeError, ValueError):
                state = "invalidated"
            if state == "waiting":
                continue
            watched.pop(symbol, None)
            self.store.set("watch_candidates", watched)  # Consume before any possible order.
            if state == "triggered":
                try:
                    decision = DecisionProposal.parse(candidate.decision, policy, snapshot)
                    self.queue(decision, "watch", time.time(), min(candidate.expires, now+self.cfg["command_ttl_seconds"]))
                    self.store.event("watch_triggered", {"symbol": symbol, "reason": candidate.reason})
                except (KeyError, TypeError, ValueError):
                    self.store.event("watch_invalidated", {"symbol": symbol, "reason": "觸發後行情或停損條件不再有效"})
                    self.store.set("last_analysis", 0)
            else:
                self.store.event("watch_invalidated", {"symbol": symbol, "reason": state})
                self.store.set("last_analysis", 0)

    @staticmethod
    def market_issues(snapshot, policy):
        issues = []
        for symbol in policy.symbols:
            market = snapshot["symbols"].get(symbol)
            if not isinstance(market, dict):
                issues.append(symbol + " 未出現在 EA 行情快照；請在市場報價顯示該商品")
                continue
            if market.get("ready") is True:
                continue
            clues = []
            commission = market.get("commission_round_turn")
            if not isinstance(commission, (int, float)) or commission < 0:
                clues.append("佣金未知")
            bid, ask = market.get("bid"), market.get("ask")
            if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)) or bid <= 0 or ask <= bid:
                clues.append("買賣報價無效")
            bars = market.get("bars")
            if isinstance(bars, dict):
                counts = {tf: len(bars.get(tf) or []) for tf in ("M5", "M15", "H1", "H4")}
                clues.append("已完成 K 棒 " + "、".join(tf + " " + str(count) + " 根" for tf, count in counts.items()))
            clues.append("EA 尚未確認報價新鮮且所需 K 棒齊全")
            issues.append(symbol + " 未就緒（" + "；".join(clues) + "）")
        return issues

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

    def pending_policy(self):
        identifier = self.store.get("pending", "")
        row = self.store.db.execute(
            "SELECT data FROM proposals WHERE id=? AND kind='policy' AND status='pending' AND expires>?",
            (identifier, time.time())).fetchone()
        return (identifier, json.loads(row["data"])) if row else ("", None)

    def reject_policy(self, identifier):
        pending, _ = self.pending_policy()
        if not pending or pending != identifier:
            raise ValueError("草案已過期、已使用或不屬於目前帳號")
        with self.store.db:
            changed = self.store.db.execute(
                "UPDATE proposals SET status='rejected' WHERE id=? AND kind='policy' AND status='pending' AND base_version=? AND expires>?",
                (identifier, self.version(), time.time())).rowcount
            if changed != 1:
                raise ValueError("草案已過期或策略版本已變更")
            self.store.db.execute("UPDATE state SET value=? WHERE key='pending' AND value=?",
                                  (dumps(""), dumps(identifier)))
        self.store.event("proposal_rejected", {"id": identifier, "reason": "使用者不接受草案"})
        self.publish()
        return "已取消這份策略草案；原本已確認策略與交易狀態沒有改變。可直接說新想法。"

    @staticmethod
    def clear_draft_revision(message):
        if any(mark in message for mark in ("？", "?")) or re.search(r"(?:可不可以|能不能|會不會|怎麼|為什麼|如何|什麼)", message):
            return False
        return bool(re.search(r"(?:改成|改為|改用|換成|調整|修改|增加|加入|移除|刪掉|刪除|不要|只做)", message) or
                    re.match(r"^(?:我想要|我希望|我要)\s*(?:每筆|單筆|總風險|固定|只|交易|進場|出場|停損|商品|週期)", message))

    def conversation_history(self, count=6):
        rows = self.store.db.execute(
            "SELECT data FROM events WHERE kind='conversation' ORDER BY id DESC LIMIT ?", (count,)).fetchall()
        history = []
        for row in reversed(rows):
            item = json.loads(row[0])
            history.append({"user": str(item.get("question", ""))[:500],
                            "assistant": str(item.get("answer", ""))[:1200]})
        return history

    @staticmethod
    def policy_preview(policy, before, identifier):
        labels = {"title": "名稱", "instructions": "適用範圍", "direction": "交易方向",
                  "symbols": "商品", "timeframes": "週期", "entry": "進場條件",
                  "invalidation": "失效條件", "management": "持倉管理", "definitions": "術語定義",
                  "risk_pct": "單筆風險", "total_risk_pct": "總持倉風險",
                  "daily_loss_pct": "每日損失上限", "drawdown_pct": "總回撤上限",
                  "risk_mode": "單筆計算方式", "risk_amount": "單筆停損金額", "fixed_lots": "固定手數"}
        changed = [labels[k] for k, v in policy.to_dict().items() if k != "version" and before.get(k) != v]
        direction = {"BUY": "只做多", "SELL": "只做空", "BOTH": "多空皆可"}[policy.direction]
        def paragraph(value):
            return re.sub(r"。(?=\S)", "。\n", value.strip())
        single = ("單筆 " + str(policy.risk_pct) + "%" if policy.risk_mode == "percent" else
                  "每筆停損最多 " + str(policy.risk_amount) + " 帳戶幣別" if policy.risk_mode == "cash" else
                  "每筆固定 " + str(policy.fixed_lots) + " 手")
        lines = ["策略草案｜" + policy.title, "版本 " + str(policy.version) + "；尚未套用，也不會直接交易。",
                 "商品：" + "、".join(policy.symbols), "方向：" + direction,
                 "分析週期：" + "、".join(policy.timeframes),
                 "風險：" + single + "／總持倉 " + str(policy.total_risk_pct) +
                 "%／每日損失 " + str(policy.daily_loss_pct) + "%／總回撤 " + str(policy.drawdown_pct) + "%"]
        if policy.risk_mode == "fixed_lots":
            lines.append("固定手數若超出商品規格、總風險或可用保證金，EA 會拒絕該筆交易。")
        for label, value in (("適用範圍", policy.instructions), ("進場條件", policy.entry),
                             ("何時失效", policy.invalidation), ("進場後如何管理", policy.management),
                             ("術語如何定義", policy.definitions)):
            lines.extend(("", label + "：", paragraph(value)))
        lines.extend(("", "本次變更：" + ("、".join(changed) if before else "首次建立"),
                      "有疑問可直接回覆；明確說出要改的條件，系統會產生新版草案。",
                      "按『接受草案』只保存策略，之後仍須另行確認『啟動』；不接受可按『不接受草案』。草案 10 分鐘後過期。",
                      "確認 " + identifier))
        return "\n".join(lines)

    def revise_draft(self, detail):
        identifier, pending = self.pending_policy()
        if not detail.strip():
            raise ValueError("請在『修改』後說明要改的策略條件")
        if not pending and not self.policy():
            raise ValueError("目前沒有策略；請先說明要建立的交易方法與商品")
        risk = self.risk_request(detail.strip())
        if risk:
            return self.risk_draft(*risk, detail.strip())
        if not pending:
            return self.draft(detail.strip(), discussion_context=True)
        return self.draft("以待確認草案為底稿，保留未要求更動的規則與商品（" +
                          "、".join(pending["symbols"]) + "）；使用者修改要求：" + detail.strip(),
                          pending_policy=pending, discussion_context=True)

    @staticmethod
    def risk_request(message):
        value = r"(\d+(?:\.\d+)?)\s*%"
        cash = r"(\d+(?:\.\d+)?)\s*(?:USD|美元|美金|帳戶幣別)?"
        lots = r"(\d+(?:\.\d+)?)\s*手"
        patterns = (("risk_amount", rf"(?:(?:每筆|單筆)\s*)?(?:停損\s*)?(?:最多\s*)?(?:虧損|虧|損失)\s*(?:最多\s*)?{cash}"),
                    ("fixed_lots", rf"(?:(?:每筆|單筆)\s*)?(?:固定\s*)?{lots}"),
                    ("total_risk_pct", rf"(?:總持倉風險|總風險|合計風險)\s*{value}"),
                    ("daily_loss_pct", rf"(?:每日損失|每日虧損|日損)\s*{value}"),
                    ("drawdown_pct", rf"(?:總回撤|最大回撤|回撤)\s*{value}"),
                    ("risk_pct", rf"(?:(?:每筆|單筆)(?:交易)?\s*)?(?:風險\s*)?{value}\s*(?:風險)?"))
        for field, pattern in patterns:
            match = re.fullmatch(pattern, message)
            if match:
                return field, float(match.group(1))
        return None

    def risk_draft(self, field, amount, original):
        limit = 1e12 if field == "risk_amount" else 100000 if field == "fixed_lots" else 100
        minimum = 0.00000001 if field == "fixed_lots" else 0.01
        if not minimum <= amount <= limit:
            raise ValueError("風險數值須大於零且在可表示範圍內")
        if field == "risk_amount":
            currency = self.snapshot().get("currency", "")
            if not currency:
                raise ValueError("EA 尚未提供帳戶幣別；請等候快照更新後再設定金額")
            if re.search(r"USD|美元|美金", original, re.IGNORECASE) and currency != "USD":
                raise ValueError("此帳戶幣別是 " + currency + "，不能把美元金額直接當成帳戶幣別；請用帳戶幣別重新指定")
        _, pending = self.pending_policy()
        current = self.policy()
        before = pending or (current.to_dict() if current else None)
        if not before:
            if field not in {"risk_pct", "total_risk_pct", "risk_amount", "fixed_lots"}:
                raise ValueError("請先建立策略，再設定此項風險百分比")
            return self.auto_draft(original, risk_change=(field, amount))
        if field == "risk_pct" and amount > before["total_risk_pct"]:
            raise ValueError("單筆風險不可高於總持倉風險；請先提高總風險，或改用較低的單筆數值")
        if field == "total_risk_pct" and before.get("risk_mode", "percent") == "percent" and amount < before["risk_pct"]:
            raise ValueError("總持倉風險不可低於單筆風險；請先降低單筆風險")
        changes = {field: amount}
        if field in {"risk_pct", "risk_amount", "fixed_lots"}:
            changes |= {"risk_mode": {"risk_pct": "percent", "risk_amount": "cash",
                                      "fixed_lots": "fixed_lots"}[field],
                        "risk_amount": amount if field == "risk_amount" else 0.0,
                        "fixed_lots": amount if field == "fixed_lots" else 0.0}
        policy = StrategyPolicy.parse(before | changes, self.version()+1)
        identifier = self.propose("policy", policy.to_dict())
        return self.policy_preview(policy, before, identifier)

    def auto_draft(self, detail="", risk_change=None):
        percentages = re.findall(r"(\d+(?:\.\d+)?)\s*%", detail)
        if len(percentages) > 1:
            raise ValueError("一次請設定一種風險數值；分別說明每筆或總持倉風險")
        per_trade, total = None, None
        if percentages:
            amount = float(percentages[0])
            if "總風險" in detail or "總持倉風險" in detail or "合計風險" in detail:
                if not 0.01 <= amount <= 100:
                    raise ValueError("總持倉風險須在 0.01% 至 100% 之間")
                total = amount
            else:
                if not 0.01 <= amount <= 100:
                    raise ValueError("單筆風險須在 0.01% 至 100% 之間")
                per_trade = amount
        instruction = ("請根據目前可分析商品的已完成 K 棒，自行選擇適合的交易方法與商品；"
                       "可以比較趨勢、突破、區間或結構，但須明確寫出各方法何時適用、進場確認、失效與退出條件。"
                       "把它整理成可檢閱的策略卡；後續每次分析可在已確認方法內擇優或觀望，"
                       "不得自行新增方法、改變方向或風控。" + ("使用者補充：" + detail if detail else ""))
        return self.draft(instruction, auto=True, per_trade=per_trade, total=total, risk_change=risk_change)

    def draft(self, instruction, auto=False, per_trade=None, total=None, pending_policy=None,
              risk_change=None, discussion_context=False, required_timeframes=()):
        snapshot = self.snapshot()
        if snapshot.get("ea_version") != REQUIRED_EA_VERSION:
            raise ValueError("請先在 MT5 重新掛載 v1.013 EA，再建立策略")
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
        if discussion_context:
            request["discussion_history"] = self.conversation_history()
        if pending_policy:
            request["pending_policy"] = pending_policy
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
        previous = pending_policy or (current.to_dict() if current else {})
        requested = set()
        if re.search(r"(?:總持倉風險|總風險|合計風險)[^\d%]{0,12}\d+(?:\.\d+)?\s*%", instruction):
            requested.add("total_risk_pct")
        if re.search(r"(?:每日損失|每日虧損|日損)[^\d%]{0,12}\d+(?:\.\d+)?\s*%", instruction):
            requested.add("daily_loss_pct")
        if re.search(r"(?:總回撤|最大回撤|回撤)[^\d%]{0,12}\d+(?:\.\d+)?\s*%", instruction):
            requested.add("drawdown_pct")
        if re.search(r"(?:每筆|單筆)(?:交易|風險)?[^\d%]{0,12}\d+(?:\.\d+)?\s*%", instruction):
            requested.update(("risk_pct", "risk_mode", "risk_amount", "fixed_lots"))
        if re.search(r"\d+(?:\.\d+)?\s*手|(?:虧|損失)\s*\d+(?:\.\d+)?", instruction):
            requested.update(("risk_mode", "risk_amount", "fixed_lots"))
        for key, default in (("risk_pct", 0.5), ("total_risk_pct", 1.5),
                             ("daily_loss_pct", 2.0), ("drawdown_pct", 5.0),
                             ("risk_mode", "percent"), ("risk_amount", 0.0), ("fixed_lots", 0.0)):
            if key not in requested:
                proposed[key] = previous.get(key, default)
        if per_trade is not None:
            proposed = proposed | {"risk_pct": per_trade, "risk_mode": "percent",
                                   "risk_amount": 0.0, "fixed_lots": 0.0}
        if total is not None:
            proposed = proposed | {"total_risk_pct": total}
        if risk_change:
            field, amount = risk_change
            proposed[field] = amount
            if field in {"risk_pct", "risk_amount", "fixed_lots"}:
                proposed |= {"risk_mode": {"risk_pct": "percent", "risk_amount": "cash",
                                          "fixed_lots": "fixed_lots"}[field],
                             "risk_amount": amount if field == "risk_amount" else 0.0,
                             "fixed_lots": amount if field == "fixed_lots" else 0.0}
        policy = StrategyPolicy.parse(proposed, self.version()+1)
        if set(required_timeframes) - set(policy.timeframes):
            raise ValueError("AI 草案未保留選定方案的分析週期；草案未建立")
        if policy.risk_mode == "cash":
            currency = snapshot.get("currency", "")
            if not currency:
                raise ValueError("EA 尚未提供帳戶幣別；無法確認金額風險")
            if re.search(r"USD|美元|美金", instruction, re.IGNORECASE) and currency != "USD":
                raise ValueError("此帳戶幣別是 " + currency + "，不能把美元金額直接當成帳戶幣別")
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
        before = pending_policy or (current.to_dict() if current else {})
        return self.policy_preview(policy, before, identifier)

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
            self.clear_watches()
            self.store.set("last_analysis", 0)
        elif row["kind"] == "resume":
            snapshot = self.snapshot()
            if snapshot.get("ea_version") != REQUIRED_EA_VERSION:
                raise ValueError("請先在 MT5 重新掛載 v1.013 EA，再啟動新單")
            p = self.policy()
            if self.cfg.get("account_mode", "demo") == "real" and not self.cfg.get("live_enabled", False):
                raise ValueError("此實盤帳號尚未授權自動新單")
            if not p or snapshot.get("halted") or snapshot.get("state_ok") is not True:
                raise ValueError("尚無策略或 EA 風控鎖定，不能啟動")
            issues = self.market_issues(snapshot, p)
            if issues:
                raise ValueError("行情尚未就緒：" + "；".join(issues) + "。這次確認碼已使用；行情恢復後請再傳『啟動』取得新確認碼")
            self.store.set("paused", False)
            self.store.set("resume_nonce", self.store.get("resume_nonce", 0)+1)
            self.store.set("last_analysis", 0)
        elif row["kind"] == "close":
            snapshot = self.snapshot()
            self.store.set("paused", True)
            self.clear_watches()
            for pos in snapshot["positions"]:
                if pos["owned"] and pos["id"] in data["ids"]:
                    self.queue(DecisionProposal("CLOSE", pos["symbol"], "使用者確認平倉", position_id=str(pos["id"])), "manual", time.time())
        elif row["kind"] == "reset":
            snapshot = self.snapshot()
            if any(p["owned"] for p in snapshot["positions"]):
                raise ValueError("有本系統持倉，不能重設總回撤")
            self.store.set("paused", True)
            self.clear_watches()
            self.store.set("reset_nonce", self.store.get("reset_nonce", 0)+1)
        self.store.event("confirmed", {"id": identifier, "kind": row["kind"]})
        self.publish()
        return "已套用。" + ("目前暫停新單。" if self.store.get("paused", True) else ("實盤" if self.cfg.get("account_mode", "demo") == "real" else "模擬") + "自動交易已啟用。")

    def handle(self, message):
        message = message.strip()
        if message == "查看策略":
            policy = self.policy()
            pending_id, pending = self.pending_policy()
            if not policy and not pending:
                return "尚無已確認策略或待確認草案。可直接討論，或傳『自動模式』建立草案。"
            parts = []
            if policy:
                parts.append("已確認策略｜" + policy.title + " v" + str(policy.version) +
                             "\n商品：" + "、".join(policy.symbols) +
                             "\n方向：" + {"BUY": "只做多", "SELL": "只做空", "BOTH": "多空皆可"}[policy.direction] +
                             "\n週期：" + "、".join(policy.timeframes) +
                             "\n單筆風險：" + (str(policy.risk_pct) + "%" if policy.risk_mode == "percent" else
                                              str(policy.risk_amount) + " 帳戶幣別" if policy.risk_mode == "cash" else
                                              str(policy.fixed_lots) + " 手") +
                             "\n狀態：" + ("暫停新單" if self.store.get("paused", True) else "自動交易已啟用"))
            if pending:
                parts.append("待確認草案｜" + str(pending.get("title", "未命名")) +
                             "\n尚未套用；可傳『查看草案』檢查完整內容。")
            return "\n\n".join(parts)
        if message == "查看草案":
            identifier, data = self.pending_policy()
            if not data:
                return "目前沒有有效的待確認策略草案。"
            policy = StrategyPolicy.parse(data, self.version()+1)
            current = self.policy()
            return self.policy_preview(policy, current.to_dict() if current else {}, identifier)
        if message in {"/start", "/help", "說明"}:
            return "可以直接和 AI 討論策略、風險與固定手數；討論不會改動交易設定。\n自動模式：AI 依目前商品行情提出交易方法。\n策略 用SMC只做空…\n整理成草案 你的方案｜修改：你的要求\n狀態｜持倉｜原因｜暫停｜啟動｜平倉｜重設回撤\n確認 <提案ID>\n草案確認後仍須另行確認啟動。"
        if message in {"自動模式", "/auto"} or message.startswith(("自動模式 ", "/auto ")):
            detail = message.split(" ", 1)[1].strip() if " " in message else ""
            return self.auto_draft(detail)
        risk = self.risk_request(message)
        if risk:
            return self.risk_draft(*risk, message)
        if message.startswith(("策略 ", "/strategy ")):
            return self.draft(message.split(" ", 1)[1])
        revision = re.fullmatch(r"(?:修改|調整|改成)[：:]?\s+(.+)|(?:修改|調整|改成)[：:]\s*(.+)", message)
        if revision:
            return self.revise_draft(next(part for part in revision.groups() if part is not None))
        draft_request = re.fullmatch(r"(?:幫我)?整理成草案(?:[：:]\s*|\s+)(.+)", message)
        if message in {"整理成草案", "幫我整理成草案", "整理成草案：", "幫我整理成草案："}:
            return "請說明要採用的商品、方法與風險，例如『整理成草案 XAUUSD 高頻剝頭皮，每筆 1%』。"
        if draft_request:
            detail = draft_request.group(1).strip()
            if not detail:
                return "請說明要採用的商品、方法與風險，例如『整理成草案 XAUUSD 高頻剝頭皮，每筆 1%』。"
            return self.draft(detail, discussion_context=True)
        if message.startswith(("確認 ", "/confirm ")):
            identifier = message.split(" ", 1)[1].strip()
            try:
                return self.confirm(identifier)
            except ValueError as exc:
                self.store.event("proposal_rejected", {"id": identifier[:64], "reason": str(exc)})
                raise
        if message in {"接受草案", "不接受草案"} or message.startswith("拒絕草案 "):
            identifier = message.split(" ", 1)[1].strip() if message.startswith("拒絕草案 ") else self.pending_policy()[0]
            if not identifier:
                raise ValueError("目前沒有有效的待確認策略草案")
            return self.confirm(identifier) if message == "接受草案" else self.reject_policy(identifier)
        if message in {"暫停", "/pause"}:
            self.store.set("paused", True)
            self.clear_watches()
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
            if p:
                issues = self.market_issues(s, p)
                parts.append("策略商品行情：" + ("已就緒。" if not issues else "；".join(issues) + "。"))
                watched = self.store.get("watch_candidates", {})
                parts.append("待觸發機會：" + ("、".join(watched) if watched else "無") + "。")
            parts.append("AI：" + self.api_status + "。")
            return "\n".join(parts)
        if message in {"原因", "/why"}:
            parts = []
            p = self.policy()
            if not p:
                parts.append("尚未確認策略，所以不會開新單。")
            else:
                parts.append("目前" + ("暫停新單" if self.store.get("paused", True) else "已啟用自動交易") + "；已確認策略：" + p.title + "。")
                try:
                    issues = self.market_issues(self.snapshot(), p)
                    if issues:
                        parts.append("目前行情阻止啟動：" + "；".join(issues) + "。")
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    parts.append("目前無法核對 MT5 快照：" + str(exc)[:180] + "。")
            recent = []
            for event in reversed(self.store.recent(30)):
                data = event["data"]
                if event["kind"] == "proposal_rejected":
                    recent.append("確認未套用：" + str(data.get("reason", "原因未記錄"))[:400])
                elif event["kind"] == "confirmed" and data.get("kind") == "policy":
                    recent.append("策略卡已確認保存；這不等於啟動交易。")
                elif event["kind"] == "decision":
                    decision = data.get("decision", {})
                    recent.append("最近 AI 決策：" + str(decision.get("symbol", "")) + " " +
                                  str(decision.get("action", "")) + "；" + str(decision.get("reason", ""))[:400])
                elif event["kind"] == "entry_review":
                    recent.append("進場前 AI 複核" + ("通過" if data.get("allow") is True else "未通過") +
                                  "：" + str(data.get("reason", "未提供理由"))[:400])
                elif event["kind"] == "watch_invalidated":
                    recent.append("候選機會失效：" + str(data.get("symbol", "")) + "；" + str(data.get("reason", ""))[:200])
                elif event["kind"] == "watch_triggered":
                    recent.append("候選機會觸發：" + str(data.get("symbol", "")) + "；進場前仍需 AI 複核。")
                if len(recent) >= 3:
                    break
            parts.extend(recent or ["目前沒有可顯示的策略或交易決策紀錄。"])
            return "\n".join(parts)
        pending_id, pending_policy = self.pending_policy()
        if pending_policy and message in {"好", "可以", "同意"}:
            return "草案尚未套用。請按『接受草案』或『不接受草案』；想改內容可以直接說明。\n確認 " + pending_id
        if pending_policy and self.clear_draft_revision(message):
            return self.revise_draft(message)
        answer = self.provider.call("chat", {"question": message, "snapshot": self.snapshot(),
                                             "policy": self.store.get("policy"), "pending_policy": pending_policy,
                                             "conversation_history": self.conversation_history()})
        reply = str(answer.get("answer", "沒有可用回答"))[:10000]
        # Some JSON-mode models double-escape line breaks inside the answer string.
        reply = reply.replace("\\r\\n", "\n").replace("\\n", "\n")
        if pending_id and self.pending_policy()[0] == pending_id:
            reply += "\n\n草案尚未套用；確認 " + pending_id
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
        if decision.action in {"BUY", "SELL"}:
            try:
                review = self.provider.call("entry_review", {"policy": p.to_dict(),
                    "decision": decision.to_dict(), "snapshot": self.ai_snapshot(snapshot, p)})
            except Exception as exc:
                if isinstance(exc, ValueError) and str(exc) == "API local cooldown active":
                    return  # Keep queued until the shared API interval or command expiry.
                with self.store.db:
                    self.store.db.execute("UPDATE commands SET status='REJECTED' WHERE id=?", (row["id"],))
                self.store.event("entry_review", {"id": row["id"], "allow": False,
                                                  "reason": "AI 複核未完成（" + type(exc).__name__ + "）"})
                return
            if (not isinstance(review, dict) or type(review.get("allow")) is not bool or
                not isinstance(review.get("reason"), str) or not 1 <= len(review["reason"].strip()) <= 1500):
                allowed, reason = False, "AI 複核格式不正確"
            else:
                allowed, reason = review["allow"], review["reason"].strip()
            self.store.event("entry_review", {"id": row["id"], "allow": allowed,
                                              "reason": reason, "snapshot_time": snapshot["time"],
                                              "model": self.cfg["provider"]["model"]})
            if not allowed:
                with self.store.db:
                    self.store.db.execute("UPDATE commands SET status='REJECTED' WHERE id=?", (row["id"],))
                return
            try:
                latest = self.snapshot()
                before_bars = snapshot["symbols"][decision.symbol].get("bars", {})
                after_bars = latest["symbols"][decision.symbol].get("bars", {})
                if any((before_bars.get(tf) or [{}])[-1].get("time") !=
                       (after_bars.get(tf) or [{}])[-1].get("time") for tf in p.timeframes):
                    raise ValueError("複核後有新 K 棒收盤")
                if (row["expires"] < time.time() or row["version"] != self.version() or
                    self.store.get("paused", True) or latest.get("ea_version") != REQUIRED_EA_VERSION):
                    raise ValueError("複核後指令或交易權限已失效")
                DecisionProposal.parse(d, p, latest)
            except (KeyError, TypeError, ValueError, OSError) as exc:
                with self.store.db:
                    self.store.db.execute("UPDATE commands SET status='REJECTED' WHERE id=?", (row["id"],))
                self.store.event("entry_review", {"id": row["id"], "allow": False,
                                                  "reason": "AI 複核後行情已變動或不可用：" + type(exc).__name__})
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
        watched = self.store.get("watch_candidates", {})
        if set(p.symbols) <= set(watched) and not any(x["owned"] for x in snapshot["positions"]):
            return  # Local price/bar checks own every outstanding opportunity.
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
            response = self.provider.call("decisions", {"policy": p.to_dict(), "snapshot": self.ai_snapshot(snapshot, p),
                                                        "execution_context": execution_context,
                                                        "new_entries_paused": paused, "active_watches": watched,
                                                        "memory": self.store.memory()})
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
        watch_raw = response.get("watches", [])
        if not isinstance(watch_raw, list) or len(watch_raw) > len(p.symbols):
            raise ValueError("invalid watches list")
        proposed_watches = [WatchCandidate.parse(x, p, fresh, int(time.time())) for x in watch_raw]
        new_symbols = [w.symbol for w in proposed_watches]
        if len(new_symbols) != len(set(new_symbols)) or set(new_symbols) & set(watched):
            raise ValueError("duplicate or already active watch")
        if any(d.action in {"BUY", "SELL", "REVERSE"} and d.symbol in set(watched) | set(new_symbols)
               for d in decisions):
            raise ValueError("watch and immediate entry conflict")
        if proposed_watches and not paused:
            watched.update({w.symbol: w.to_dict() for w in proposed_watches})
            self.store.set("watch_candidates", watched)
            for w in proposed_watches:
                self.store.event("watch_created", {"symbol": w.symbol, "basis": w.basis,
                                                   "trigger": w.trigger_price, "invalidation": w.invalidation_price,
                                                   "expires": w.expires, "policy_version": p.version})
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
                       "|".join(p.symbols), self.store.get("reset_nonce", 0), self.store.get("resume_nonce", 0),
                       p.risk_mode, format(p.risk_amount if p.risk_mode == "cash" else
                                           p.fixed_lots if p.risk_mode == "fixed_lots" else 0.0, ".8f")])
        pending = self.store.get("pending", "")
        row = self.store.db.execute("SELECT * FROM proposals WHERE id=? AND status='pending' AND expires>?", (pending, time.time())).fetchone()
        pending_text = (row["kind"]+" "+row["id"]+"\n"+row["data"]) if row else ""
        self.bridge.csv("pending.csv", [row["id"] if row else "none"])
        events = self.store.db.execute("SELECT data FROM events WHERE kind IN ('decision','execution','error') ORDER BY id DESC LIMIT 1").fetchone()
        replies = self.store.db.execute("SELECT time,data FROM events WHERE kind='panel_reply' ORDER BY id DESC LIMIT 3").fetchall()
        def local_time(value):
            return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(value))
        chat_lines = []
        for row in reversed(replies):
            item = json.loads(row["data"])
            chat_lines.append("[" + local_time(item.get("question_time", row["time"])) + "] 你：" +
                              str(item.get("question", ""))[:1000] + "\n[" + local_time(row["time"]) + "] 回覆：" +
                              str(item.get("answer", ""))[:10000])
        chat = "\n".join(chat_lines)
        mode_label = "實盤" if self.cfg.get("account_mode", "demo") == "real" else "模擬"
        self.bridge.status({"time": int(time.time()), "headline": mode_label + " | " + ("暫停新單" if paused else "自動交易"),
                           "strategy": p.title if p else "尚無已確認策略", "api": self.api_status,
                           "chat": chat, "latest": events[0][:300] if events else "", "pending": pending_text})

    def publish_previews(self):
        """Publish only validated active watch levels; these are never broker orders."""
        policy = self.policy()
        now = int(time.time())
        records = []
        try:
            snapshot = self.snapshot()
        except (FileNotFoundError, OSError, ValueError):
            snapshot = None
        if policy and not self.store.get("paused", True):
            for symbol, raw in self.store.get("watch_candidates", {}).items():
                try:
                    watch = WatchCandidate(**raw)
                    side = watch.decision["action"]
                    if (not snapshot or snapshot.get("ea_version") != REQUIRED_EA_VERSION or
                        snapshot.get("halted") or snapshot.get("state_ok") is not True or
                        snapshot.get("symbols", {}).get(symbol, {}).get("ready") is not True or
                        watch.symbol != symbol or symbol not in policy.symbols or
                        watch.policy_version != policy.version or watch.expires <= now or
                        side not in {"BUY", "SELL"}):
                        continue
                    records.append("|".join((watch.symbol, side, watch.basis, watch.timeframe,
                                              format(watch.trigger_price, ".8f"),
                                              format(watch.invalidation_price, ".8f"), str(watch.expires))))
                except (KeyError, TypeError, ValueError):
                    continue
        self.bridge.csv("preview.csv", [1, self.cfg["account"], self.cfg["server"], self.cfg["magic"],
                                        policy.version if policy else 0, now, ";".join(records) or "-"])

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
                self.store.event("panel_reply", {"question": command, "answer": reply,
                                                 "question_time": event["time"]})
        self.store.set("ui_offset", offset)

    def tick(self):
        self.ingest()
        self.panel_events()
        self.publish()
        try:
            self.reverse_followups()
            self.monitor_watches()
            self.dispatch()
            self.analyze()
        finally:
            self.publish_previews()

    def close(self):
        self.store.db.close()
