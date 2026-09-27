"""MT5 account registry. Existing local.json remains the first independent profile."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .bridge import Bridge, atomic_write
from .service import load_config
from .storage import ProcessLock, Store


def registry_path(root: Path) -> Path:
    return root / "config" / "accounts.json"


def profile_id(account: str, server: str) -> str:
    return "demo-" + account + "-" + hashlib.sha256(server.encode("utf-8")).hexdigest()[:8]


def read_registry(root: Path) -> dict:
    path = registry_path(root)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
            raise ValueError("帳號清單格式錯誤")
        return data
    local = root / "config" / "local.json"
    if local.exists():
        cfg = json.loads(local.read_text(encoding="utf-8-sig"))
        if str(cfg.get("account", "")).isdecimal() and cfg.get("server"):
            identifier = profile_id(str(cfg["account"]), cfg["server"])
            return {"version": 1, "profiles": {identifier: "local.json"}, "shared_profile": identifier}
    return {"version": 1, "profiles": {}, "shared_profile": ""}


def save_registry(root: Path, registry: dict) -> None:
    atomic_write(registry_path(root), json.dumps(registry, ensure_ascii=False, indent=2))


def profile_path(root: Path, registry: dict, identifier: str) -> Path:
    relative = registry["profiles"][identifier]
    path = (root / "config" / relative).resolve()
    if not path.is_relative_to((root / "config").resolve()) or path.suffix != ".json":
        raise ValueError("帳號設定路徑無效")
    return path


def new_profile_config(root: Path, template: dict, account: str, server: str) -> tuple[str, Path, dict]:
    identifier = profile_id(account, server)
    path = root / "config" / "accounts" / (identifier + ".json")
    cfg = json.loads(json.dumps(template))
    cfg["account"], cfg["server"] = account, server
    cfg["account_mode"], cfg["live_enabled"] = "demo", False
    cfg["bridge_dir"] = "%APPDATA%/MetaQuotes/Terminal/Common/Files/AITrader/" + identifier
    cfg["database"] = "../../runtime/accounts/" + identifier + ".sqlite"
    return identifier, path, cfg


def load_profiles(root: Path) -> tuple[dict, dict[str, dict]]:
    registry = read_registry(root)
    if not registry["profiles"]:
        raise ValueError("請先在設定視窗新增帳號")
    profiles = {}
    bridges, databases, identities = set(), set(), set()
    for identifier in registry["profiles"]:
        cfg = load_config(profile_path(root, registry, identifier))
        if profile_id(cfg["account"], cfg["server"]) != identifier:
            raise ValueError("帳號設定與帳號代碼不符：" + identifier)
        identity = (cfg["account"], cfg["server"])
        bridge, database = os.path.normcase(cfg["bridge_dir"]), os.path.normcase(cfg["database"])
        if identity in identities or bridge in bridges or database in databases:
            raise ValueError("多帳號設定重複使用帳號、通訊目錄或資料庫")
        identities.add(identity); bridges.add(bridge); databases.add(database)
        profiles[identifier] = cfg
    shared = registry.get("shared_profile")
    if shared not in profiles:
        raise ValueError("找不到共用 API/Telegram 設定")
    for cfg in profiles.values():
        cfg["provider"] = profiles[shared]["provider"]
        cfg["telegram"] = profiles[shared]["telegram"]
    return registry, profiles


def write_mt5_index(profiles: dict[str, dict]) -> None:
    """One Common Files index lets each EA find its own profile by login/server."""
    root = None
    rows = []
    for cfg in profiles.values():
        relative = cfg["bridge_dir"].replace("\\", "/")
        marker = "/common/files/"
        at = relative.lower().find(marker)
        if at < 0:
            raise ValueError("MT5 bridge 必須位於 Common/Files")
        common = Path(cfg["bridge_dir"][:at + len(marker) - 1])
        if root is None:
            root = common
        elif os.path.normcase(str(common)) != os.path.normcase(str(root)):
            raise ValueError("多帳號必須使用相同 Windows MT5 Common Files")
        rel = relative[at + len(marker):].replace("/", "\\")
        fields = ["1", cfg["account"], cfg["server"], str(cfg["magic"]), rel,
                  cfg["ea"]["symbols"], cfg["ea"]["commission_round_turn"],
                  cfg["ea"]["max_spread_points"], cfg["ea"]["slippage_points"],
                  cfg["account_mode"], int(cfg["live_enabled"])]
        if any("|" in str(x) or "\n" in str(x) or "\r" in str(x) for x in fields):
            raise ValueError("MT5 帳號設定有不支援的字元")
        rows.append("|".join(map(str, fields)))
        Bridge(cfg["bridge_dir"]).csv("binding.csv", (1, cfg["account"], cfg["server"], cfg["magic"],
                                                    cfg["account_mode"], int(cfg["live_enabled"])))
    if root is not None:
        atomic_write(root / "AITrader" / "accounts.txt", "\n".join(rows) + "\n")


def ea_is_running(cfg: dict) -> bool:
    """Check the exclusive account lock held by the EA on Windows."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    bridge = str(cfg["bridge_dir"]).replace("\\", "/")
    marker = "/common/files/"
    at = bridge.lower().find(marker)
    if at < 0:
        raise ValueError("MT5 bridge 必須位於 Common/Files")
    common = Path(bridge[:at + len(marker) - 1])
    server_hash = 0
    encoded = cfg["server"].encode("utf-16-le")
    for at in range(0, len(encoded), 2):
        server_hash = (server_hash * 131 + int.from_bytes(encoded[at:at+2], "little")) % 2147483647
    lock = common / f'AITrader-account-{cfg["account"]}-{server_hash}-{cfg["magic"]}.lock'
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(lock), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error in {2, 3}:  # No lock file or Common Files directory.
            return False
        if error == 32:  # Sharing violation: EA still holds its exclusive lock.
            return True
        raise OSError(error, "無法確認 EA 是否仍掛載")
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle(handle)
    return False


def remove_profile(root: Path, identifier: str) -> str:
    """Unregister a flat, detached account; retain config and audit database."""
    registry, profiles = load_profiles(root)
    if identifier not in profiles:
        raise ValueError("找不到此帳號")
    if len(profiles) <= 1:
        raise ValueError("至少保留一個帳號；最後一個帳號不能移除")
    cfg = profiles[identifier]
    with ProcessLock(root / "runtime" / "multi.lock"), ProcessLock(Path(cfg["bridge_dir"]) / "service.lock"):
        if ea_is_running(cfg):
            raise ValueError("此帳號 EA 仍掛在 MT5 圖表；請先移除 EA")
        store = Store(cfg["database"])
        try:
            if store.db.execute("SELECT 1 FROM commands WHERE status IN ('queued','sent')").fetchone():
                raise ValueError("此帳號尚有待執行指令，不能移除")
            has_policy = bool(store.get("policy"))
            try:
                snapshot = Bridge(cfg["bridge_dir"]).json("snapshot.json")
            except (OSError, ValueError):
                snapshot = None
            if has_policy and not snapshot:
                raise ValueError("此帳號沒有 MT5 快照，無法確認本系統持倉已平")
            if snapshot:
                if (str(snapshot.get("account")) != cfg["account"] or snapshot.get("server") != cfg["server"] or
                    snapshot.get("magic") != cfg["magic"] or
                    snapshot.get("account_mode", "demo") != cfg["account_mode"] or
                    snapshot.get("demo") is not (cfg["account_mode"] == "demo")):
                    raise ValueError("MT5 快照帳號不符，不能移除")
                if type(snapshot.get("time")) is not int or not isinstance(snapshot.get("positions"), list):
                    raise ValueError("MT5 快照缺少持倉或時間資料，不能移除")
                if has_policy and abs(time.time() - snapshot["time"]) > 300:
                    raise ValueError("MT5 快照超過五分鐘；請先連線檢查持倉，再卸下 EA")
                if any(type(position.get("owned")) is not bool for position in snapshot["positions"]):
                    raise ValueError("MT5 持倉快照不完整，不能移除")
                if any(position.get("owned") for position in snapshot.get("positions", [])):
                    raise ValueError("此帳號仍有本系統持倉，先平倉再移除")
            store.set("paused", True)
        finally:
            store.db.close()
        remaining = {name: profile for name, profile in profiles.items() if name != identifier}
        successor = next(iter(remaining))
        if registry.get("shared_profile") == identifier:
            path = profile_path(root, registry, successor)
            target = json.loads(path.read_text(encoding="utf-8-sig"))
            target["provider"] = cfg["provider"]
            target["telegram"] = cfg["telegram"]
            atomic_write(path, json.dumps(target, ensure_ascii=False, indent=2))
        write_mt5_index(remaining)
        del registry["profiles"][identifier]
        registry["shared_profile"] = successor if registry.get("shared_profile") == identifier else registry["shared_profile"]
        save_registry(root, registry)
        return successor
