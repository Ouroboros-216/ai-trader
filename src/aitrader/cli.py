import argparse
import json
import signal
import time
from pathlib import Path

from .report import replay, report
from .provider import build_provider
from .service import Agent, load_config
from .storage import ProcessLock, dumps
from .telegram import Telegram
from .secrets import load_into_environment


def main():
    parser = argparse.ArgumentParser(description="Independent MT5 demo AI trader")
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("check")
    sub.add_parser("report")
    sub.add_parser("replay")
    sub.add_parser("api-check", help="Make one billed/quota-counted AI provider connectivity request")
    sub.add_parser("telegram-info", help="Read bot name and recent private sender IDs; no pairing or messages sent")
    message = sub.add_parser("message")
    message.add_argument("text", help="Offline operator command; stop service first, or use Telegram while running")
    args = parser.parse_args()
    load_into_environment(args.config)
    cfg = load_config(args.config)
    if args.command in {"check", "report", "replay", "api-check", "telegram-info"}:
        agent = Agent(cfg)
        try:
            if args.command == "check":
                snapshot = agent.snapshot()
                print(dumps({"bridge": "ok", "account": snapshot["account"], "demo": snapshot["demo"], "symbols": list(snapshot["symbols"]), "missing_symbols": snapshot.get("missing_symbols"), "provider_enabled": cfg["provider"]["enabled"]}))
            elif args.command == "api-check":
                client = build_provider(cfg["provider"] | {"enabled": True}, agent.store)
                print(dumps(client.call("chat", {"question": "請只回答連線成功，沒有行情資料，不作交易判斷。"})))
            elif args.command == "telegram-info":
                tg = Telegram(cfg["telegram"], agent)
                me = tg.call("getMe", {})
                updates = tg.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
                users = {m["from"]["id"]: m["chat"]["id"] for u in updates if (m := u.get("message", {})).get("chat", {}).get("type") == "private"}
                print(dumps({"bot_username": me["username"], "private_user_chat_ids": users, "note": "Send /start to your bot first. Set your own numeric ID in both config fields; no auto-pairing."}))
            else:
                print(json.dumps(report(agent.store) if args.command == "report" else replay(agent.store), ensure_ascii=False, indent=2))
        finally:
            agent.close()
        return
    with ProcessLock(Path(cfg["bridge_dir"]) / "service.lock"):
        agent = Agent(cfg)
        try:
            if args.command == "message":
                print(agent.handle(args.text))
                return
            telegram = Telegram(cfg["telegram"], agent)
            stopped = False

            def stop(*_):
                nonlocal stopped
                stopped = True

            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            last_telegram = 0
            print("AI Trader running. Account mode is checked per profile. Press Ctrl+C to stop.", flush=True)
            while not stopped:
                try:
                    if (Path(cfg["bridge_dir"]) / "stop.request").exists():
                        (Path(cfg["bridge_dir"]) / "stop.request").unlink()
                        break
                    agent.tick()
                    if time.time()-agent.store.get("last_equity_sample", 0)>=30:
                        snapshot = agent.snapshot()
                        agent.store.event("equity", {"time": snapshot["time"], "equity": snapshot["equity"]})
                        agent.store.set("last_equity_sample", time.time())
                        if not agent.store.get("paused", True) and not agent.store.get("forward_started"):
                            agent.store.set("forward_started", time.time())
                except Exception as exc:
                    agent.api_status = "暫停本輪：" + type(exc).__name__
                    agent.store.event("error", {"type": type(exc).__name__, "stage": "tick"})
                    # No exception body (may include credentials or provider text).
                try:
                    if time.time()-last_telegram>=3:
                        last_telegram = time.time()
                        telegram.poll()
                        telegram.notify_changes()
                    agent.publish()
                except Exception as exc:
                    agent.store.event("error", {"type": type(exc).__name__, "stage": "telegram/publish"})
                time.sleep(1)
        finally:
            if args.command == "run":
                agent.store.set("paused", True)
                agent.publish()
            agent.close()


if __name__ == "__main__":
    main()
