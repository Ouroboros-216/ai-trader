import os
import re
import time

from .provider import request_json


class Telegram:
    def __init__(self, config, agent, transport=request_json):
        self.config, self.agent, self.transport = config, agent, transport

    def call(self, method, payload):
        token = os.environ.get(self.config["token_env"], "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("Telegram token environment variable missing/invalid")
        response = self.transport("https://api.telegram.org/bot"+token+"/"+method, payload, timeout=10)
        if response.get("ok") is not True:
            raise ValueError("Telegram API refused request")
        return response["result"]

    def poll(self):
        if not self.config["enabled"]:
            return
        store = self.agent.store
        updates = self.call("getUpdates", {"offset": store.get("telegram_offset", 0), "timeout": 0, "limit": 10, "allowed_updates": ["message", "callback_query"]})
        for update in updates:
            identifier = update["update_id"]
            # Advance before processing: crash loses a request, never repeats a confirmation.
            store.set("telegram_offset", identifier+1)
            callback = update.get("callback_query")
            message = update.get("message", {})
            if callback:
                original = callback.get("message", {})
                message = {"from": callback.get("from", {}), "chat": original.get("chat", {}), "date": int(time.time()), "text": "確認 " + callback.get("data", "")}
            if message.get("from", {}).get("id") != self.config["user_id"] or message.get("chat", {}).get("id") != self.config["chat_id"] or message.get("chat", {}).get("type") != "private":
                continue
            if time.time()-message.get("date", 0)>120 or not isinstance(message.get("text"), str):
                continue
            try:
                command = message["text"][:8000]
                if callback and hasattr(self.agent, "callback_message"):
                    command = self.agent.callback_message(callback.get("data", ""))
                elif callback and str(callback.get("data", "")).startswith("r:"):
                    command = "拒絕草案 " + callback["data"][2:]
                reply = self.agent.handle(command)
            except Exception as exc:
                reply = "未套用操作：" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            # No parse_mode: untrusted model text cannot inject Telegram markup or links.
            for i in range(0, min(len(reply), 14000), 3500):
                payload = {"chat_id": self.config["chat_id"], "text": reply[i:i+3500]}
                pending = store.get("pending", "")
                if hasattr(self.agent, "reply_markup") and i+3500>=len(reply):
                    payload["reply_markup"] = self.agent.reply_markup(command, reply, pending)
                elif pending and pending in reply and i+3500>=len(reply):
                    data = self.agent.callback_data(pending) if hasattr(self.agent, "callback_data") else pending
                    is_policy = (hasattr(self.agent, "pending_policy") and self.agent.pending_policy()[0] == pending)
                    payload["reply_markup"] = {"inline_keyboard": [[
                        {"text": "接受草案", "callback_data": data},
                        {"text": "不接受草案", "callback_data": "r:" + pending}
                    ]] if is_policy else [[{"text": "確認此提案", "callback_data": data}]]}
                self.call("sendMessage", payload)
            if callback:
                self.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "已處理，請查看回覆"})

    def notify_changes(self):
        if not self.config["enabled"]:
            return
        store = self.agent.store
        cursor = store.get("telegram_event_cursor", 0)
        rows = store.db.execute("SELECT * FROM events WHERE id>? AND kind IN ('execution','uncertain') ORDER BY id LIMIT 5", (cursor,)).fetchall()
        for row in rows:
            self.call("sendMessage", {"chat_id": self.config["chat_id"], "text": "交易回報："+row["data"][:3000]})
            store.set("telegram_event_cursor", row["id"])
