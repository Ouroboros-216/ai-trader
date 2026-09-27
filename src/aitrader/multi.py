"""One process, one bot and independent agents for each MT5 account."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import signal
import time
import uuid
from pathlib import Path

from .accounts import load_profiles
from .contracts import StrategyPolicy
from .provider import Gemini
from .secrets import load_into_environment
from .service import Agent
from .storage import ProcessLock, Store, dumps
from .symbols import account_mapping, catalog, relevant
from .telegram import Telegram


MENU_COMMANDS = {
    "status": "狀態", "positions": "持倉", "why": "原因", "strategy": "策略", "auto": "自動模式",
    "strategy_all": "全部策略",
    "pause": "暫停", "resume": "啟動", "close": "平倉", "reset": "重設回撤",
}


class AccountRouter:
    def __init__(self, agents: dict[str, Agent], store: Store):
        self.agents, self.store = agents, store

    def account_label(self, identifier: str) -> str:
        cfg = self.agents[identifier].cfg
        return cfg["server"] + "｜" + cfg["account"]

    def selected(self):
        identifier = self.store.get("selected_account", "")
        if identifier == "__none__":
            return ""
        if identifier in self.agents:
            return identifier
        return next(iter(self.agents)) if len(self.agents) == 1 else ""

    @staticmethod
    def account_token(identifier: str) -> str:
        return hashlib.sha256(identifier.encode()).hexdigest()[:12]

    def account_from_token(self, token: str) -> str:
        matches = [name for name in self.agents if self.account_token(name) == token]
        if len(matches) != 1:
            raise ValueError("帳號按鈕無效；請重新傳『帳號』")
        return matches[0]

    def callback_data(self, pending):
        identifier = self.store.get("pending_account", "")
        if identifier == "__all__":
            return "all|" + pending
        token = self.account_token(identifier)
        return token + "|" + pending if identifier in self.agents else pending

    def callback_message(self, data: str) -> str:
        if data.startswith("a:"):
            return "帳號 " + self.account_from_token(data[2:])
        if data.startswith("m:"):
            parts = data.split(":")
            if len(parts) != 3 or parts[2] not in MENU_COMMANDS:
                raise ValueError("指令按鈕無效")
            identifier = self.account_from_token(parts[1])
            if self.selected() != identifier:
                raise ValueError("已切換帳號；請使用目前帳號的新指令按鈕")
            return MENU_COMMANDS[parts[2]]
        if data.startswith("b:"):
            identifier = self.account_from_token(data[2:])
            if self.selected() != identifier:
                raise ValueError("已切換帳號；請使用目前帳號的新返回按鈕")
            return "返回"
        return "確認 " + data

    def reply_markup(self, command: str, reply: str, pending: str) -> dict:
        if command in {"帳號", "/accounts", "/start", "返回", "/back"} or not self.selected():
            rows = [[{"text": self.account_label(identifier) + (" [實盤]" if agent.cfg.get("account_mode") == "real" else ""),
                      "callback_data": "a:" + self.account_token(identifier)}]
                    for identifier, agent in self.agents.items()]
            if pending and pending in reply and self.store.get("pending_account") == "__all__":
                rows.insert(0, [{"text": "確認套用全部策略", "callback_data": self.callback_data(pending)}])
            return {"inline_keyboard": rows}
        identifier = self.selected()
        token = self.account_token(identifier)
        def button(key):
            return {"text": MENU_COMMANDS[key], "callback_data": "m:" + token + ":" + key}
        rows = [[button("status"), button("positions")],
                [button("why"), button("strategy")],
                [button("auto")],
                [button("strategy_all")],
                [button("pause"), button("resume")],
                [button("close"), button("reset")]]
        if pending and pending in reply:
            label = "確認套用全部策略" if self.store.get("pending_account") == "__all__" else "確認此提案"
            rows.insert(0, [{"text": label, "callback_data": self.callback_data(pending)}])
        rows.append([{"text": "返回帳號清單", "callback_data": "b:" + token}])
        return {"inline_keyboard": rows}

    @staticmethod
    def mapped_symbols(agent: Agent) -> tuple[dict[str, str], str]:
        try:
            snapshot = agent.snapshot()
        except (OSError, ValueError, KeyError, TypeError):
            return {}, "EA 快照未就緒"
        try:
            discovered = catalog(agent, snapshot)
            source = "市場報價商品清單" if (agent.bridge.root / "catalog.json").exists() else "EA 快照"
            return account_mapping(discovered), source
        except (OSError, ValueError):
            return account_mapping(list(snapshot["symbols"])), "EA 快照"

    def draft_all(self, instruction: str) -> str:
        if not instruction.strip() or len(instruction) > 4000:
            raise ValueError("請在『策略全部』後輸入完整交易規則（最多 4000 字）")
        mappings = {name: self.mapped_symbols(agent) for name, agent in self.agents.items()}
        requested_sets = [set(mapping) for mapping, _ in mappings.values()]
        common = sorted(set.intersection(*requested_sets))
        if not common:
            raise ValueError("帳號沒有共同商品名稱；請先核對各帳號交易商品，再分別建立策略")
        common = relevant(common, instruction)
        source = self.agents[self.selected()] if self.selected() else next(iter(self.agents.values()))
        result = source.provider.call("strategy", {"request": instruction, "current_policy": None,
                                                   "available_symbols": common,
                                                   "account_count": len(self.agents),
                                                   "note": "只產生共用交易邏輯；風險保留各帳號既有設定，且不啟動交易"})
        if result.get("questions") or not result.get("policy"):
            return "需要釐清：" + dumps(result.get("questions", ["策略資料不足"]))
        template = StrategyPolicy.parse(result["policy"], 1)
        if set(template.symbols) - set(common):
            raise ValueError("AI 策略含有非所有帳號共通的商品，草案未建立")
        lowered = instruction.lower()
        if any(x in lowered for x in ("只做空", "只放空", "only sell", "short only")) and template.direction != "SELL":
            raise ValueError("AI 未遵守只做空要求，草案未建立")
        if any(x in lowered for x in ("只做多", "only buy", "long only")) and template.direction != "BUY":
            raise ValueError("AI 未遵守只做多要求，草案未建立")
        plan = []
        for identifier, agent in self.agents.items():
            mapping, source_label = mappings[identifier]
            data = template.to_dict() | {"version": agent.version()+1,
                                         "symbols": [mapping[s] for s in template.symbols]}
            previous = agent.policy()
            if previous:
                for key in ("risk_pct", "total_risk_pct", "daily_loss_pct", "drawdown_pct"):
                    data[key] = getattr(previous, key)
            policy = StrategyPolicy.parse(data, agent.version()+1)
            plan.append({"account_id": identifier, "base_version": agent.version(),
                         "policy": policy.to_dict(), "symbol_source": source_label})
        lines = ["全部帳號策略草案（確認後各帳號仍暫停新單）：",
                 "共用的是交易規則；實際風險、商品名稱、點差與佣金依各帳號資料。",
                 dumps(template.to_dict())]
        for item in plan:
            p = item["policy"]
            mode = "[實盤]" if self.agents[item["account_id"]].cfg.get("account_mode") == "real" else "[模擬]"
            lines.append(self.account_label(item["account_id"])+mode+"：商品="+",".join(p["symbols"])+"；映射="+item["symbol_source"]+
                         "；版本="+str(p["version"])+"；單筆/總風險="+str(p["risk_pct"])+"%/"+str(p["total_risk_pct"])+"%")
        if len("\n".join(lines)) > 13500:
            raise ValueError("全部策略卡過長，請縮短規則或分組設定帳號")
        proposal = uuid.uuid4().hex
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='superseded' WHERE kind='batch_policy' AND status='pending'")
            self.store.db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,'pending')",
                                  (proposal, 0, time.time()+600, "batch_policy", dumps(plan)))
        self.store.set("pending", proposal)
        self.store.set("pending_account", "__all__")
        return "\n".join(lines) + "\n確認 all|" + proposal

    def confirm_all(self, proposal: str) -> str:
        row = self.store.db.execute("SELECT * FROM proposals WHERE id=? AND kind='batch_policy'", (proposal,)).fetchone()
        if not row or row["status"] != "pending" or row["expires"] < time.time():
            raise ValueError("全部策略提案已過期或已使用")
        plan = json.loads(row["data"])
        if set(item["account_id"] for item in plan) != set(self.agents) or len(plan) != len(self.agents):
            raise ValueError("帳號清單已變更，請重新建立全部策略草案")
        # Complete every read-only preflight before changing any account.
        for item in plan:
            agent = self.agents[item["account_id"]]
            if agent.version() != item["base_version"]:
                raise ValueError(self.account_label(item["account_id"])+" 策略版本已變，請重新建立草案")
            StrategyPolicy.parse(item["policy"], item["base_version"]+1)
            if agent.store.db.execute("SELECT 1 FROM commands WHERE status IN ('queued','sent')").fetchone():
                raise ValueError(self.account_label(item["account_id"])+" 尚有待執行指令，不能批量改策略")
            if agent.policy():
                try:
                    snapshot = agent.snapshot()
                except (OSError, ValueError) as exc:
                    raise ValueError(self.account_label(item["account_id"])+" 既有策略帳號的 EA 快照未就緒；先連線再批量改策略") from None
                if any(position["owned"] for position in snapshot["positions"]):
                    raise ValueError(self.account_label(item["account_id"])+" 有本系統持倉，先平倉後再批量改策略")
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='applying' WHERE id=? AND status='pending'", (proposal,))
        applied = []
        try:
            for item in plan:
                self.agents[item["account_id"]].store.set("paused", True)
            for item in plan:
                agent = self.agents[item["account_id"]]
                agent.store.set("policy", item["policy"])
                agent.store.event("confirmed", {"batch_id": proposal, "kind": "policy", "policy": item["policy"]})
                agent.publish()
                applied.append(item["account_id"])
        except Exception:
            for agent in self.agents.values():
                try:
                    agent.store.set("paused", True)
                    agent.publish()
                except Exception:
                    pass
            with self.store.db:
                self.store.db.execute("UPDATE proposals SET status='partial' WHERE id=?", (proposal,))
            raise ValueError("批量套用中斷；已套用帳號："+",".join(self.account_label(i) for i in applied)+"。所有帳號請保持暫停並核對策略") from None
        with self.store.db:
            self.store.db.execute("UPDATE proposals SET status='confirmed' WHERE id=?", (proposal,))
        self.store.set("pending", "")
        self.store.set("pending_account", "")
        return "已將策略套用至 "+str(len(applied))+" 個帳號；各帳號目前暫停新單，須個別確認啟動交易。"

    def resolve_account(self, value: str) -> str:
        matches = []
        for identifier, agent in self.agents.items():
            display = identifier + "：" + agent.cfg["account"] + " @ " + agent.cfg["server"]
            if value in {identifier, agent.cfg["account"], agent.cfg["server"], display, self.account_label(identifier)}:
                matches.append(identifier)
        if len(matches) > 1:
            raise ValueError("帳號或伺服器不唯一；請點清單按鈕或回覆完整『券商｜帳號』")
        return matches[0] if matches else ""

    def handle(self, message: str) -> str:
        message = message.strip()
        if message.startswith(("策略全部 ", "/strategy_all ")):
            return self.draft_all(message.split(" ", 1)[1])
        if message in {"全部策略", "/strategy_all"}:
            return "傳『策略全部 你的交易規則』建立所有帳號的待確認草案；套用後各帳號保持暫停新單。"
        if message.startswith("確認 all|"):
            return self.confirm_all(message[7:].strip())
        if message in {"帳號", "/accounts", "/start"}:
            rows = []
            for identifier, agent in self.agents.items():
                marker = "→ " if self.selected() == identifier else "  "
                rows.append(marker + self.account_label(identifier) +
                            (" [實盤]" if agent.cfg.get("account_mode") == "real" else ""))
            return "可用帳號：\n" + "\n".join(rows) + "\n點下方帳號，或直接回覆帳號數字。"
        if message in {"返回", "/back"}:
            self.store.set("selected_account", "__none__")
            self.store.set("pending", "")
            self.store.set("pending_account", "")
            return "已返回帳號清單；目前未選擇操作帳號。服務仍持續管理各帳號。"
        explicit = message.startswith(("帳號 ", "/account "))
        choice = message.split(" ", 1)[1].strip() if explicit else message
        chosen = self.resolve_account(choice)
        if explicit and not chosen:
            raise ValueError("找不到此帳號；傳『帳號』查看清單")
        if chosen:
            identifier = chosen
            self.store.set("selected_account", identifier)
            self.store.set("pending", "")
            self.store.set("pending_account", "")
            return "已選擇 " + self.account_label(identifier) + "。可點下方指令；按『返回帳號清單』可退出。"
        identifier = self.selected()
        if message.startswith("確認 ") and "|" in message:
            routed, proposal = message[3:].strip().split("|", 1)
            if len(proposal) != 32:
                raise ValueError("提案帳號或 ID 無效")
            identifier = self.account_from_token(routed)
            message = "確認 " + proposal
        if not identifier:
            return "請先傳『帳號』查看清單，再點帳號按鈕或回覆帳號數字。"
        if message in {"策略", "/strategy"}:
            return "【" + self.account_label(identifier) + "】\n傳『策略 你的交易規則』建立待確認策略卡；例如『策略 用 SMC 只做空』。"
        agent = self.agents[identifier]
        try:
            reply = agent.handle(message)
        except ValueError as exc:
            raise ValueError("帳號 " + self.account_label(identifier) + "：" + str(exc)) from None
        pending = agent.store.get("pending", "")
        if pending and pending in reply:
            self.store.set("pending", pending)
            self.store.set("pending_account", identifier)
        else:
            self.store.set("pending", "")
        return "【" + self.account_label(identifier) + "】\n" + reply


class MultiTelegram(Telegram):
    def install_commands(self):
        if not self.config["enabled"]:
            return
        commands = [
            ("start", "顯示帳號選單"), ("accounts", "選擇操作帳號"),
            ("status", "查看目前帳號狀態"), ("positions", "查看持倉"),
            ("why", "查看最近決策原因"), ("strategy", "查看策略輸入方式"),
            ("auto", "讓 AI 提出自選交易方法"),
            ("strategy_all", "全部帳號共用策略草案"),
            ("pause", "暫停新單"), ("resume", "提出啟動交易"),
            ("close", "提出平倉"), ("reset", "提出重設回撤"),
            ("back", "返回帳號清單"),
        ]
        self.call("setMyCommands", {
            "commands": [{"command": name, "description": description} for name, description in commands],
            "scope": {"type": "chat", "chat_id": self.config["chat_id"]},
        })

    def notify_changes(self):
        if not self.config["enabled"]:
            return
        for identifier, agent in self.agent.agents.items():
            store = agent.store
            cursor = store.get("telegram_event_cursor", 0)
            rows = store.db.execute("SELECT * FROM events WHERE id>? AND kind IN ('execution','uncertain') ORDER BY id LIMIT 5", (cursor,)).fetchall()
            for row in rows:
                self.call("sendMessage", {"chat_id": self.config["chat_id"], "text": "【" + self.agent.account_label(identifier) + "】交易回報：" + row["data"][:2900]})
                store.set("telegram_event_cursor", row["id"])


def tick_agent(agent):
    try:
        agent.tick()
        if time.time() - agent.store.get("last_equity_sample", 0) >= 30:
            snapshot = agent.snapshot()
            agent.store.event("equity", {"time": snapshot["time"], "equity": snapshot["equity"]})
            agent.store.set("last_equity_sample", time.time())
            if not agent.store.get("paused", True) and not agent.store.get("forward_started"):
                agent.store.set("forward_started", time.time())
    except Exception as exc:
        agent.api_status = "暫停本輪：" + type(exc).__name__
        agent.store.event("error", {"type": type(exc).__name__, "stage": "tick"})


def recover_incomplete_batches(agents: dict[str, Agent], router_store: Store):
    rows = router_store.db.execute("SELECT id FROM proposals WHERE kind='batch_policy' AND status='applying'").fetchall()
    if not rows:
        return
    for agent in agents.values():
        agent.store.set("paused", True)
    for agent in agents.values():
        try:
            agent.publish()
        except Exception as exc:
            agent.store.event("error", {"type": type(exc).__name__, "stage": "batch_recovery_publish"})
    with router_store.db:
        router_store.db.execute("UPDATE proposals SET status='partial' WHERE kind='batch_policy' AND status='applying'")
    router_store.set("pending", "")
    router_store.set("pending_account", "")
    router_store.event("error", {"stage": "batch_recovery", "count": len(rows), "reason": "interrupted batch; all accounts paused"})


def run(root: Path):
    _, profiles = load_profiles(root)
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    stop_file = runtime / "stop.request"
    with contextlib.ExitStack() as stack:
        stack.enter_context(ProcessLock(runtime / "multi.lock"))
        for cfg in profiles.values():
            stack.enter_context(ProcessLock(Path(cfg["bridge_dir"]) / "service.lock"))
        quota = Store(runtime / "shared-api.sqlite")
        router_store = Store(runtime / "telegram-router.sqlite")
        stack.callback(quota.db.close)
        stack.callback(router_store.db.close)
        agents = {}
        for identifier, cfg in profiles.items():
            agent = Agent(cfg)
            # One API key means one global quota/backoff across all accounts.
            agent.provider = Gemini(cfg["provider"], agent.store, quota_store=quota)
            agents[identifier] = agent
            stack.callback(agent.close)
        recover_incomplete_batches(agents, router_store)
        router = AccountRouter(agents, router_store)
        telegram = MultiTelegram(next(iter(profiles.values()))["telegram"], router)
        try:
            telegram.install_commands()
        except Exception as exc:
            router_store.event("error", {"type": type(exc).__name__, "stage": "telegram_commands"})
        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        print("AI Trader multi-account service running: " + ", ".join(agents), flush=True)
        last_telegram = 0
        while not stopped:
            if stop_file.exists():
                stop_file.unlink()
                break
            for agent in agents.values():
                tick_agent(agent)
            if time.time() - last_telegram >= 3:
                last_telegram = time.time()
                try:
                    telegram.poll()
                    telegram.notify_changes()
                except Exception as exc:
                    router_store.event("error", {"type": type(exc).__name__, "stage": "telegram"})
            for agent in agents.values():
                try:
                    agent.publish()
                except Exception as exc:
                    agent.store.event("error", {"type": type(exc).__name__, "stage": "publish"})
            time.sleep(1)
        for agent in agents.values():
            agent.store.set("paused", True)
            agent.publish()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "check"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    load_into_environment(args.root / "config" / "local.json")
    if args.command == "check":
        _, profiles = load_profiles(args.root)
        print("Validated Demo profiles: " + ", ".join(profiles))
    else:
        run(args.root)


if __name__ == "__main__":
    main()
