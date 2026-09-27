"""Small Windows setup window; no third-party UI dependencies."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from contextlib import ExitStack
from pathlib import Path
from tkinter import messagebox, ttk

from .bridge import Bridge, atomic_write
from .contracts import MarketSnapshot
from .provider import Gemini
from .secrets import load_into_environment, read_secrets, save_secrets
from .storage import ProcessLock, Store
from .telegram import Telegram
from .updater import current_version, latest_release, version_tuple
from .accounts import (load_profiles, new_profile_config, profile_id, profile_path,
                       read_registry, remove_profile, save_registry, write_mt5_index)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "local.json"
SECRETS = ROOT / "config" / "secrets.bin"


def initial_config(path=CONFIG):
    target = path if path.exists() else ROOT / "config" / "example.json"
    return json.loads(target.read_text(encoding="utf-8-sig"))


def account_labels(root: Path, registry: dict) -> dict[str, str]:
    """Human-readable choices; profile IDs remain the internal identity."""
    labels = {}
    for identifier in registry["profiles"]:
        cfg = json.loads(profile_path(root, registry, identifier).read_text(encoding="utf-8-sig"))
        labels[identifier] = f'{cfg["server"]}｜{cfg["account"]}' + ("｜實盤" if cfg.get("account_mode", "demo") == "real" else "")
    return labels


def valid_form(account: str, server: str, model: str, user_id: str):
    if not account.isdecimal():
        raise ValueError("請輸入帳號數字")
    if not server.strip() or any(c in server for c in ',|\r\n"'):
        raise ValueError("請輸入完整 MT5 伺服器名稱")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
        raise ValueError("請填入 AI Studio 顯示的模型 ID")
    if user_id and (not user_id.isdecimal() or int(user_id) <= 0):
        raise ValueError("Telegram ID 必須是正整數")


def valid_ea(symbols: str, commissions: str):
    names = [s.strip() for s in symbols.split(",")]
    costs = [s.strip() for s in commissions.split(",")]
    if not 1 <= len(names) <= 10 or len(costs) not in {1, len(names)} or len(set(names)) != len(names):
        raise ValueError("佣金填一個共用值，或依商品順序各填一個；商品不能重複")
    if any(not re.fullmatch(r"[A-Za-z0-9_.#-]+", name) for name in names):
        raise ValueError("商品請填 MT5 中的精確名稱，以逗號分隔")
    if any(not re.fullmatch(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", cost) for cost in costs):
        raise ValueError("佣金只能填數字；未知時填 -1")
    if any(float(cost) < 0 and float(cost) != -1 for cost in costs):
        raise ValueError("未知佣金只能填 -1")
    return ",".join(names), ",".join(costs)


def bridge_relative(value: str) -> str:
    normalized = value.replace("\\", "/")
    marker = "/common/files/"
    idx = normalized.lower().find(marker)
    if idx < 0:
        raise ValueError("bridge_dir 必須在 MT5 Common/Files 資料夾內")
    suffix = normalized[idx + len(marker):].strip("/")
    if not suffix or any(part in {"", ".", ".."} for part in suffix.split("/")):
        raise ValueError("bridge_dir 的相對路徑無效")
    return suffix.replace("/", "\\")


def save_form(config_path: Path, account: str, server: str, model: str, user_id: str,
              gemini_ok: bool, telegram_ok: bool, new_key: str, new_token: str,
              symbols: str = "XAUUSD,EURUSD,GBPUSD", commissions: str = "-1",
              secrets_path: Path | None = None, account_mode: str = "demo", live_enabled: bool = False) -> dict:
    valid_form(account, server, model, user_id)
    if account_mode not in {"demo", "real"} or type(live_enabled) is not bool or (account_mode == "demo" and live_enabled):
        raise ValueError("帳戶模式或實盤授權設定無效")
    symbols, commissions = valid_ea(symbols, commissions)
    config = json.loads((config_path if config_path.exists() else config_path.with_name("example.json")).read_text(encoding="utf-8-sig"))
    bridge_input = bridge_relative(config["bridge_dir"])
    bridge_root = Path(os.path.expandvars(config["bridge_dir"]))
    if not bridge_root.is_absolute():
        raise ValueError("MT5 共用資料夾路徑無法解析")
    secrets_file = secrets_path or config_path.with_name("secrets.bin")
    saved = read_secrets(secrets_file)
    if gemini_ok and not (new_key or saved.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")):
        raise ValueError("Gemini 已勾選連線成功，但找不到 API key")
    if telegram_ok and not (new_token or saved.get("AI_TRADER_TELEGRAM_TOKEN") or os.environ.get("AI_TRADER_TELEGRAM_TOKEN")):
        raise ValueError("Telegram 已勾選連線成功，但找不到 bot token")
    config["account"] = account
    config["server"] = server.strip()
    config["account_mode"] = account_mode
    config["live_enabled"] = live_enabled
    config.setdefault("ea", {})["symbols"] = symbols
    config["ea"]["commission_round_turn"] = commissions
    config["ea"].setdefault("max_spread_points", "80,25,30")
    config["ea"].setdefault("slippage_points", "10,3,3")
    if len(symbols.split(",")) != len(config["ea"]["max_spread_points"].split(",")):
        config["ea"]["max_spread_points"] = ",".join("25" for _ in symbols.split(","))
    if len(symbols.split(",")) != len(config["ea"]["slippage_points"].split(",")):
        config["ea"]["slippage_points"] = ",".join("10" for _ in symbols.split(","))
    config["provider"]["model"] = model.strip()
    config["provider"]["enabled"] = bool(gemini_ok)
    config["telegram"]["user_id"] = int(user_id) if user_id else 0
    config["telegram"]["chat_id"] = int(user_id) if user_id else 0
    config["telegram"]["enabled"] = bool(telegram_ok and user_id)
    # Protect secrets before writing a config that enables their use.
    save_secrets(secrets_file, {"GEMINI_API_KEY": new_key.strip() or (os.environ.get("GEMINI_API_KEY", "") if not saved.get("GEMINI_API_KEY") else ""),
                                "AI_TRADER_TELEGRAM_TOKEN": new_token.strip() or (os.environ.get("AI_TRADER_TELEGRAM_TOKEN", "") if not saved.get("AI_TRADER_TELEGRAM_TOKEN") else "")})
    atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2))
    preset = ["InpBridge=" + bridge_input, "InpDemoLogin=" + account, "InpDemoServer=" + server.strip(),
              "InpMagic=" + str(config["magic"]), "InpSymbols=" + symbols,
              "InpCommissionRoundTurn=" + commissions,
              "InpMaxSpreadPoints=" + config["ea"]["max_spread_points"],
              "InpSlippagePoints=" + config["ea"]["slippage_points"], "InpBars=100"]
    atomic_write(config_path.with_name("AITrader.generated.set"), "\n".join(preset))
    Bridge(bridge_root).csv("binding.csv", (1, account, server.strip(), config["magic"], account_mode, int(live_enabled)))
    return config


class SetupWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # Build first; avoid a half-painted window.
        self.root.title("AI Trader — 設定")
        self.root.geometry("790x900")
        self.root.minsize(740, 850)
        self.registry = read_registry(ROOT)
        self.account_labels = account_labels(ROOT, self.registry)
        self.selected_id = next(iter(self.registry["profiles"]), "")
        self.profile_config = profile_path(ROOT, self.registry, self.selected_id) if self.selected_id else CONFIG
        self.new_account = not bool(self.selected_id)
        config = initial_config(self.profile_config)
        shared_config = initial_config(profile_path(ROOT, self.registry, self.registry["shared_profile"])) if self.registry.get("shared_profile") in self.registry["profiles"] else config
        load_into_environment(CONFIG)
        saved = read_secrets(SECRETS)
        self.vars = {
            "account": tk.StringVar(value=config.get("account", "")),
            "server": tk.StringVar(value=config.get("server", "")),
            "symbols": tk.StringVar(value=config.get("ea", {}).get("symbols", "XAUUSD,EURUSD,GBPUSD")),
            "commissions": tk.StringVar(value=config.get("ea", {}).get("commission_round_turn", "-1")),
            "account_mode": tk.StringVar(value="實盤" if config.get("account_mode", "demo") == "real" else "模擬"),
            "model": tk.StringVar(value=shared_config["provider"].get("model", "")),
            "gemini_key": tk.StringVar(),
            "telegram_token": tk.StringVar(),
            "user_id": tk.StringVar(value=str(shared_config["telegram"].get("user_id") or "")),
        }
        self.gemini_ok = bool(shared_config["provider"].get("enabled"))
        self.telegram_ok = bool(shared_config["telegram"].get("enabled"))
        self.verified_ids = [str(shared_config["telegram"].get("user_id"))] if self.telegram_ok else []
        self.saved_hint = {"gemini": bool(saved.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")),
                           "telegram": bool(saved.get("AI_TRADER_TELEGRAM_TOKEN") or os.environ.get("AI_TRADER_TELEGRAM_TOKEN"))}
        self.status = tk.StringVar(value="先填資料，測試連線，最後按「儲存設定」。")
        self.account_choice = tk.StringVar(value=self.account_labels.get(self.selected_id, ""))
        self._build()
        for name in ("model", "gemini_key"):
            self.vars[name].trace_add("write", lambda *_: self._invalidate("gemini"))
        self.vars["telegram_token"].trace_add("write", lambda *_: self._invalidate("telegram"))
        self.vars["user_id"].trace_add("write", lambda *_: self._select_user())

    def _invalidate(self, which):
        if which == "gemini":
            self.gemini_ok = False
        else:
            self.telegram_ok = False
            self.verified_ids = []

    def _select_user(self):
        self.telegram_ok = self.vars["user_id"].get().strip() in self.verified_ids

    def _build(self):
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="AI Trader 設定", font=("Microsoft JhengHei", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        ttk.Label(frame, text="① MT5 帳戶", font=("Microsoft JhengHei", 11, "bold")).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 5))
        self._field(frame, 2, "帳號", "account")
        self._field(frame, 3, "完整伺服器名稱", "server")
        ttk.Label(frame, text="帳戶類型").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=5)
        self.mode_box = ttk.Combobox(frame, textvariable=self.vars["account_mode"], values=["模擬", "實盤"], state="readonly", width=16)
        self.mode_box.grid(row=4, column=1, sticky="w", pady=5)
        ttk.Label(frame, text="交易商品由策略對話選擇；系統搜尋券商商品，遇到多個後綴會請你選擇。", wraplength=730).grid(row=6, column=0, columnspan=3, sticky="w", pady=5)
        self._field(frame, 7, "每手開平合計佣金", "commissions")
        ttk.Label(frame, text="佣金填一個數值套用此帳戶全部商品；未知填 -1。可先接 MT5 再讀取券商規則。", wraplength=730).grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(frame, text="② AI API", font=("Microsoft JhengHei", 11, "bold")).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 5))
        self._field(frame, 10, "模型 ID", "model")
        self._field(frame, 11, "API key", "gemini_key", secret=True)
        ttk.Label(frame, text="目前支援 Gemini；金鑰留白沿用已儲存金鑰，測試會使用一次 API 配額。", wraplength=730).grid(row=12, column=0, columnspan=2, sticky="w")
        ttk.Button(frame, text="測試 Gemini", command=self.test_gemini).grid(row=12, column=2, sticky="e", pady=6)

        ttk.Label(frame, text="③ 第三方軟體", font=("Microsoft JhengHei", 11, "bold")).grid(row=13, column=0, columnspan=3, sticky="w", pady=(8, 5))
        self._field(frame, 14, "飛機 Bot token", "telegram_token", secret=True)
        ttk.Label(frame, text="先向飛機 bot 傳 /start，再按右方讀取 ID；從清單選你自己的數字 ID。", wraplength=730).grid(row=15, column=0, columnspan=2, sticky="w")
        ttk.Button(frame, text="讀取飛機 ID", command=self.test_telegram).grid(row=15, column=2, sticky="e", pady=6)
        ttk.Label(frame, text="你的使用者 ID").grid(row=16, column=0, sticky="w", padx=(0, 10), pady=5)
        self.user_box = ttk.Combobox(frame, textvariable=self.vars["user_id"])
        self.user_box.grid(row=16, column=1, columnspan=2, sticky="ew", pady=5)

        ttk.Separator(frame).grid(row=17, column=0, columnspan=3, sticky="ew", pady=10)
        for row, actions in [(18, [("儲存設定", self.save), ("檢查 MT5", self.check_mt5),
                                   ("讀取 MT5 佣金", self.read_commissions)]),
                             (19, [("啟動服務", self.start_service), ("停止服務", self.stop_service),
                                   ("檢查更新", self.check_update), ("開啟完整說明", self.open_guide)])]:
            buttons = ttk.Frame(frame)
            buttons.grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 5))
            for title, callback in actions:
                ttk.Button(buttons, text=title, command=callback).pack(side="left", padx=(0, 8))
        ttk.Label(frame, textvariable=self.status, foreground="navy", wraplength=740).grid(row=20, column=0, columnspan=3, sticky="w", pady=12)
        ttk.Label(frame, text="金鑰儲存在此 Windows 使用者的加密檔案；不會寫進 local.json 或 ZIP。\nEA 會核對帳號、伺服器與帳戶類型；交易前仍需確認策略與啟動。", wraplength=740).grid(row=21, column=0, columnspan=3, sticky="w")
        accounts = ttk.Frame(frame)
        accounts.grid(row=22, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(accounts, text="券商／帳號：").pack(side="left")
        self.account_box = ttk.Combobox(accounts, textvariable=self.account_choice,
                                        values=list(self.account_labels.values()), state="readonly", width=38)
        self.account_box.pack(side="left", padx=5)
        self.account_box.bind("<<ComboboxSelected>>", self._choose_account)
        ttk.Button(accounts, text="新增帳號", command=self._new_account).pack(side="left", padx=5)
        ttk.Button(accounts, text="移除帳號", command=self.remove_account).pack(side="left", padx=5)
        ttk.Label(frame, text="移除前先停止服務並卸下該帳號 EA；設定檔與交易紀錄會保留。", wraplength=740).grid(row=23, column=0, columnspan=3, sticky="w")

    def _new_account(self):
        self.new_account = True
        self.account_choice.set("")
        self.vars["account"].set("")
        self.vars["server"].set("")
        self.vars["account_mode"].set("模擬")
        self.status.set("填入新帳號、伺服器、帳戶類型與佣金，再按『儲存設定』。現有帳號資料不變。")

    def _choose_account(self, *_):
        identifier = next((key for key, label in self.account_labels.items() if label == self.account_choice.get()), "")
        if identifier not in self.registry["profiles"]:
            return
        self.selected_id = identifier
        self.profile_config = profile_path(ROOT, self.registry, identifier)
        self.new_account = False
        config = initial_config(self.profile_config)
        shared_id = self.registry.get("shared_profile", identifier)
        shared = initial_config(profile_path(ROOT, self.registry, shared_id))
        values = {"account": config["account"], "server": config["server"],
                  "symbols": config["ea"]["symbols"], "commissions": config["ea"]["commission_round_turn"],
                  "model": shared["provider"]["model"],
                  "account_mode": "實盤" if config.get("account_mode", "demo") == "real" else "模擬",
                  "user_id": str(shared["telegram"].get("user_id") or "")}
        for name, value in values.items():
            self.vars[name].set(value)
        self.gemini_ok = shared["provider"]["enabled"]
        self.telegram_ok = shared["telegram"]["enabled"]
        self.status.set("已選擇 " + self.account_labels[identifier] + "；修改佣金後按『儲存設定』。更換登入帳號請按『新增帳號』。")

    def remove_account(self):
        identifier = next((key for key, label in self.account_labels.items() if label == self.account_choice.get()), "")
        if self.new_account or identifier not in self.registry["profiles"]:
            messagebox.showerror("無法移除", "請先從帳號清單選擇要移除的帳號")
            return
        if not messagebox.askyesno("移除帳號", "要從服務清單移除 " + identifier + " 嗎？\n\n請先停止服務、平掉本系統持倉並從 MT5 圖表卸下此 EA。原設定檔與交易紀錄會保留，其他帳號不受影響。"):
            return
        try:
            successor = remove_profile(ROOT, identifier)
            self.registry = read_registry(ROOT)
            self.account_labels = account_labels(ROOT, self.registry)
            self.account_box.configure(values=list(self.account_labels.values()))
            self.account_choice.set(self.account_labels[successor])
            self._choose_account()
            self.status.set("已從服務清單移除 " + identifier + "。原設定及交易紀錄仍保留；重新啟動服務後套用。")
        except Exception as exc:
            detail = "服務仍在運行；請先按『停止服務』並稍候再移除" if isinstance(exc, RuntimeError) else str(exc) if isinstance(exc, (ValueError, OSError)) else type(exc).__name__
            messagebox.showerror("無法移除", detail)

    def _field(self, frame, row, label, key, secret=False):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
        widget = ttk.Entry(frame, textvariable=self.vars[key], show="●" if secret else "")
        widget.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)

    def _run(self, label, function, done):
        self.status.set(label + "：測試中…")
        def worker():
            try:
                result = function()
            except Exception as exc:
                if self.root.winfo_exists():
                    error = str(exc) if isinstance(exc, ValueError) and str(exc).startswith("Gemini HTTP ") else (
                        "HTTP " + str(exc.code) if hasattr(exc, "code") else type(exc).__name__)
                    self.root.after(0, lambda: self.status.set(label + "失敗：" + error + "。檢查憑證、模型、網路或配額。"))
                return
            if self.root.winfo_exists():
                self.root.after(0, lambda: done(result))
        threading.Thread(target=worker, daemon=True).start()

    def test_gemini(self):
        model = self.vars["model"].get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
            messagebox.showerror("缺少模型", "先填 AI Studio 的模型 ID。")
            return
        key = self.vars["gemini_key"].get().strip() or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            messagebox.showerror("缺少金鑰", "先填 Gemini API key。")
            return
        def call():
            os.environ["GEMINI_API_KEY"] = key
            cfg = initial_config()["provider"] | {"model": model, "enabled": True, "api_key_env": "GEMINI_API_KEY"}
            store = Store(ROOT / "runtime" / "shared-api.sqlite")
            try:
                result = Gemini(cfg, store).call("chat", {"question": "請回覆連線成功。沒有行情資料，不作交易判斷。"})
                if not isinstance(result.get("answer"), str) or not result["answer"].strip():
                    raise ValueError("沒有有效回答")
                return model
            finally:
                store.db.close()
        def done(checked_model):
            if self.vars["model"].get().strip() == checked_model and self.vars["gemini_key"].get().strip() in {"", key}:
                self.gemini_ok = True
                self.status.set("Gemini 連線成功。按「儲存設定」後會啟用 Gemini。")
        self._run("Gemini", call, done)

    def test_telegram(self):
        token = self.vars["telegram_token"].get().strip() or os.environ.get("AI_TRADER_TELEGRAM_TOKEN", "")
        if not token:
            messagebox.showerror("缺少 token", "先填 Telegram bot token，並向 bot 傳 /start。")
            return
        def call():
            os.environ["AI_TRADER_TELEGRAM_TOKEN"] = token
            tg = Telegram({"enabled": True, "token_env": "AI_TRADER_TELEGRAM_TOKEN"}, None)
            me = tg.call("getMe", {})
            updates = tg.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
            ids = sorted({str(m["from"]["id"]) for u in updates if (m := u.get("message", {})).get("chat", {}).get("type") == "private" and m.get("from", {}).get("id") == m["chat"].get("id")})
            return me.get("username", "bot"), ids
        def done(result):
            if self.vars["telegram_token"].get().strip() not in {"", token}:
                return
            username, ids = result
            self.verified_ids = ids
            self.user_box.configure(values=ids)
            self._select_user()
            self.status.set("@"+username+" 連線成功。" + ("請從 ID 清單選擇你的 ID，再儲存設定。" if ids else "尚未收到私人 /start；傳訊息後再按一次。"))
        self._run("Telegram", call, done)

    def save(self):
        try:
            account = self.vars["account"].get().strip()
            server = self.vars["server"].get().strip()
            identifier = profile_id(account, server)
            account_mode = "real" if self.vars["account_mode"].get() == "實盤" else "demo"
            live_enabled = account_mode == "real"
            if self.new_account:
                if identifier in self.registry["profiles"]:
                    raise ValueError("此帳號已存在；請從帳號清單選取")
                if not self.registry["profiles"]:
                    target = CONFIG
                else:
                    _, target, profile = new_profile_config(ROOT, initial_config(self.profile_config), account, server)
                    atomic_write(target, json.dumps(profile, ensure_ascii=False, indent=2))
            else:
                if identifier != self.selected_id:
                    raise ValueError("既有帳號不可直接改登入與伺服器；請按『新增帳號』")
                target = self.profile_config
                saved_mode = initial_config(target).get("account_mode", "demo")
                if account_mode != saved_mode:
                    raise ValueError("此帳號已綁定「"+("實盤" if saved_mode == "real" else "模擬")+"」；請用『新增帳號』加入另一個 MT5 帳號")
            cfg = save_form(target, account, server,
                            self.vars["model"].get().strip(), self.vars["user_id"].get().strip(),
                            self.gemini_ok, self.telegram_ok, self.vars["gemini_key"].get(), self.vars["telegram_token"].get(),
                            self.vars["symbols"].get(), self.vars["commissions"].get(), secrets_path=SECRETS,
                            account_mode=account_mode, live_enabled=live_enabled)
            if self.new_account:
                self.registry["profiles"][identifier] = str(target.relative_to(ROOT / "config")).replace("\\", "/")
            self.registry["shared_profile"] = identifier
            save_registry(ROOT, self.registry)
            _, profiles = load_profiles(ROOT)
            write_mt5_index(profiles)
            self.selected_id, self.profile_config, self.new_account = identifier, target, False
            self.account_labels = account_labels(ROOT, self.registry)
            self.account_choice.set(self.account_labels[identifier])
            self.account_box.configure(values=list(self.account_labels.values()))
            load_into_environment(CONFIG)
            self.vars["gemini_key"].set("")
            self.vars["telegram_token"].set("")
            # Clearing masked fields after save is a UI action, not a credential change.
            self.gemini_ok = cfg["provider"]["enabled"]
            self.telegram_ok = cfg["telegram"]["enabled"]
        except Exception as exc:
            messagebox.showerror("設定未儲存", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            return False
        self.status.set("多帳號設定已儲存。請在此帳號 MT5 視窗重新掛載新版 EA，服務重啟後套用。Gemini="+str(self.gemini_ok)+"，Telegram="+str(self.telegram_ok)+"。")
        return True

    def check_mt5(self):
        try:
            cfg = initial_config(self.profile_config)
            account = self.vars["account"].get().strip()
            server = self.vars["server"].get().strip()
            root = Path(os.path.expandvars(cfg["bridge_dir"]))
            if not root.is_absolute():
                root = (CONFIG.parent / root).resolve()
            raw = Bridge(root).json("snapshot.json")
            data = MarketSnapshot.parse(raw, account, server, cfg["magic"], time.time(), cfg["snapshot_max_age_seconds"], cfg.get("account_mode", "demo")).data
            ready = [s for s, v in data["symbols"].items() if v.get("ready")]
            mode = "實盤" if cfg.get("account_mode", "demo") == "real" else "模擬"
            self.status.set("MT5 "+mode+"帳戶已連接；帳號 "+account+"，可用商品："+(", ".join(ready) or "行情尚未就緒"))
            return True
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            self.status.set("MT5 尚未連上：" + detail + "。請核對 EA 已掛載、帳號類型、伺服器與 InpBridge。")
            return False

    def read_commissions(self):
        try:
            cfg = initial_config(self.profile_config)
            root = Path(os.path.expandvars(cfg["bridge_dir"]))
            if not root.is_absolute():
                root = (CONFIG.parent / root).resolve()
            raw = Bridge(root).json("snapshot.json")
            data = MarketSnapshot.parse(raw, self.vars["account"].get().strip(), self.vars["server"].get().strip(), cfg["magic"], time.time(), cfg["snapshot_max_age_seconds"], cfg.get("account_mode", "demo")).data
            names = list(data["symbols"])
            if not names:
                raise ValueError("EA 尚未提供策略商品報價")
            values = []
            for symbol in names:
                market = data["symbols"].get(symbol)
                if not market or market.get("commission_source") != "broker_rule" or market.get("commission_round_turn", -1) < 0:
                    raise ValueError(symbol + "：券商沒有提供可安全換算的固定佣金規則")
                values.append(float(market["commission_round_turn"]))
            display = [format(v, ".8g") for v in values]
            if len(set(display)) == 1:
                display = display[:1]
            elif names != self.vars["symbols"].get().split(","):
                raise ValueError("策略商品佣金各不相同；須按商品個別設定，不能填一個共用值")
            suggestion = ",".join(display)
            if messagebox.askyesno("採用 MT5 佣金", "券商規則換算的每手開平合計佣金："+suggestion+"（帳戶貨幣）\n\n單一數值會套用全部策略商品。要帶入欄位嗎？"):
                self.vars["commissions"].set(suggestion)
                self.status.set("已帶入 MT5 佣金規則；按「儲存設定」，並重新掛載 EA。")
        except Exception as exc:
            self.status.set("無法自動帶入佣金：" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__) + "。可查帳戶成交紀錄或向券商確認。")

    def start_service(self):
        try:
            if not self.save():
                return
            _, profiles = load_profiles(ROOT)
            if not next(iter(profiles.values()))["provider"]["enabled"]:
                raise ValueError("請先成功測試 Gemini，然後儲存設定")
            root = ROOT / "runtime"
            root.mkdir(exist_ok=True)
            with ExitStack() as locks:
                locks.enter_context(ProcessLock(root / "multi.lock"))
                for cfg in profiles.values():
                    locks.enter_context(ProcessLock(Path(cfg["bridge_dir"]) / "service.lock"))
                (root / "stop.request").unlink(missing_ok=True)
            out = (root / "service.out.log").open("a", encoding="utf-8")
            err = (root / "service.err.log").open("a", encoding="utf-8")
            try:
                process = subprocess.Popen([sys.executable, "-m", "aitrader.multi", "run", "--root", str(ROOT)],
                                           cwd=ROOT, stdout=out, stderr=err,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                out.close(); err.close()
            self.status.set("服務已啟動（PID "+str(process.pid)+"），已載入 "+str(len(profiles))+" 個帳號。尚未掛 EA 的帳號會等待連線；可用『檢查 MT5』核對目前帳號。")
        except Exception as exc:
            messagebox.showerror("無法啟動", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)

    def stop_service(self):
        try:
            root = ROOT / "runtime"
            if not root.is_dir():
                raise ValueError("服務尚未安裝")
            (root / "stop.request").write_text("stop", encoding="ascii")
            # Also stop a still-running single-account service during upgrade.
            for identifier in self.registry["profiles"]:
                cfg = initial_config(profile_path(ROOT, self.registry, identifier))
                bridge_root = Path(os.path.expandvars(cfg["bridge_dir"]))
                if bridge_root.is_dir():
                    (bridge_root / "stop.request").write_text("stop", encoding="ascii")
            self.status.set("已要求服務正常停止。既有持倉不會因此平倉。")
        except Exception as exc:
            messagebox.showerror("無法停止", str(exc) if isinstance(exc, ValueError) else type(exc).__name__)

    def check_update(self):
        def done(release):
            local = current_version(ROOT)
            if version_tuple(release["version"]) <= version_tuple(local):
                self.status.set("目前已是最新版（v" + local + "）。")
                return
            if not messagebox.askyesno("安裝更新", "找到 v" + release["version"] + "（目前 v" + local + "）。\n\n"
                                   "要下載並校驗 GitHub Release，停止服務、更新程式後自動重啟嗎？\n"
                                   "本機帳號、金鑰與交易紀錄會保留。尚未儲存的欄位請先取消並儲存。"):
                self.status.set("已取消更新。")
                return
            root = ROOT / "runtime"
            root.mkdir(exist_ok=True)
            out = (root / "update.out.log").open("a", encoding="utf-8")
            err = (root / "update.err.log").open("a", encoding="utf-8")
            try:
                subprocess.Popen([sys.executable, "-m", "aitrader.updater", "apply", "--root", str(ROOT), "--tag", release["tag"]],
                                 cwd=ROOT, stdout=out, stderr=err,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                out.close()
                err.close()
            self.status.set("更新程序已啟動；完成後設定視窗會自動重新開啟。")
            self.root.after(1000, self.root.destroy)
        self._run("GitHub 更新", lambda: latest_release(ROOT), done)

    def open_guide(self):
        os.startfile(ROOT / "docs" / "SETUP.zh-TW.md")

    def run(self):
        self.root.deiconify()
        self.root.mainloop()


def main():
    SetupWindow().run()


if __name__ == "__main__":
    main()
