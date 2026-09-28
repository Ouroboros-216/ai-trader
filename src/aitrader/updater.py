"""Verified GitHub Release updates for the installed source bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .storage import ProcessLock


DEFAULT_REPO = "Ouroboros-216/ai-trader"
MAX_DOWNLOAD = 100 * 1024 * 1024
REPLACE_ROOT = {"README.md", "pyproject.toml", "開啟設定.cmd"}
REPLACE_DIRS = {"src", "scripts", "docs", "tests", "mql5"}


def current_version(root: Path) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", value):
        raise ValueError("GitHub 發布版本格式無效")
    return tuple(map(int, value.removeprefix("v").split(".")))


def repo_name(root: Path) -> str:
    path = root / "config" / "update.json"
    repo = json.loads(path.read_text(encoding="utf-8"))["repo"] if path.exists() else DEFAULT_REPO
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("更新來源設定無效")
    return repo


def _read_url(url: str, max_bytes: int = MAX_DOWNLOAD) -> bytes:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AITrader-Updater"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("更新檔超過安全大小限制")
    return data


def latest_release(root: Path, read_url=_read_url) -> dict:
    repo = repo_name(root)
    raw = json.loads(read_url(f"https://api.github.com/repos/{repo}/releases/latest", 2 * 1024 * 1024))
    tag = str(raw.get("tag_name", ""))
    version_tuple(tag)
    name = f"AITrader-{tag}.zip"
    assets = {item.get("name"): item.get("browser_download_url") for item in raw.get("assets", [])}
    if name not in assets or name.removesuffix(".zip") + ".sha256" not in assets:
        raise ValueError("GitHub 發布缺少 ZIP 或 SHA-256 檔")
    for url in (assets[name], assets[name.removesuffix(".zip") + ".sha256"]):
        if not isinstance(url, str) or not url.startswith(f"https://github.com/{repo}/releases/download/{tag}/"):
            raise ValueError("GitHub 更新網址與設定的倉庫不符")
    return {"tag": tag, "version": tag.removeprefix("v"), "zip_url": assets[name],
            "sha_url": assets[name.removesuffix(".zip") + ".sha256"], "zip_name": name}


def _archive_files(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    result = []
    total = 0
    for item in archive.infolist():
        if item.is_dir():
            continue
        if "\\" in item.filename or item.filename.startswith("/"):
            raise ValueError("更新 ZIP 路徑無效")
        path = PurePosixPath(item.filename)
        if len(path.parts) < 2 or path.parts[0] != "ai-trader" or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("更新 ZIP 路徑無效")
        relative = Path(*path.parts[1:])
        if not (relative.as_posix() in REPLACE_ROOT or
                (relative.parts[0] == "config" and relative.parts == ("config", "example.json")) or
                relative.parts[0] in REPLACE_DIRS):
            raise ValueError("更新 ZIP 包含不允許覆蓋的檔案")
        if item.file_size > 15 * 1024 * 1024:
            raise ValueError("更新 ZIP 包含異常大的檔案")
        total += item.file_size
        if total > MAX_DOWNLOAD:
            raise ValueError("更新 ZIP 解壓後超過安全大小限制")
        result.append((item, relative))
    if not {Path("pyproject.toml"), Path("src/aitrader/setup_gui.py"), Path("src/aitrader/updater.py")} <= {p for _, p in result}:
        raise ValueError("更新 ZIP 缺少必要程式")
    if len({p for _, p in result}) != len(result):
        raise ValueError("更新 ZIP 有重複路徑")
    return result


def verified_stage(root: Path, release: dict, read_url=_read_url) -> Path:
    checksum = read_url(release["sha_url"], 4096).decode("ascii").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([A-Za-z0-9_.-]+\.zip)", checksum)
    if not match or match.group(2) != release["zip_name"]:
        raise ValueError("SHA-256 檔格式或檔名不符")
    data = read_url(release["zip_url"])
    if hashlib.sha256(data).hexdigest().lower() != match.group(1).lower():
        raise ValueError("更新 ZIP 校驗失敗")
    stage = Path(tempfile.mkdtemp(prefix="aitrader-update-", dir=root / "runtime"))
    zip_path = stage / "package.zip"
    zip_path.write_bytes(data)
    with zipfile.ZipFile(zip_path) as archive:
        files = _archive_files(archive)
        for item, relative in files:
            destination = stage / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    if current_version(stage / "files") != release["version"]:
        raise ValueError("ZIP 內版本與 GitHub 發布標籤不符")
    return stage


def _service_running(root: Path) -> bool:
    try:
        with ProcessLock(root / "runtime" / "multi.lock"):
            return False
    except OSError:
        return True


def installed_ea_targets(appdata: Path | None = None) -> list[Path]:
    """Find only terminals where AITrader is already installed for this Windows user."""
    location = appdata if appdata is not None else Path(os.environ.get("APPDATA", ""))
    if not location.is_absolute():
        return []
    base = location / "MetaQuotes" / "Terminal"
    if not base.is_dir():
        return []
    resolved = base.resolve()
    targets = []
    for terminal in base.iterdir():
        target = terminal / "MQL5" / "Experts" / "AITrader" / "AITrader.ex5"
        if terminal.is_dir() and target.is_file() and target.resolve().is_relative_to(resolved):
            targets.append(target)
    if len(targets) > 20:
        raise ValueError("找到超過 20 個 MT5 EA 安裝位置；請先檢查終端機資料夾")
    return targets


def sync_bundled_ea(root: Path, appdata: Path | None = None) -> int:
    """Bridge upgrades launched by the pre-v0.8.7 updater, which copied app files only."""
    source = root / "mql5" / "AITrader.ex5"
    if not source.is_file():
        raise ValueError("安裝包缺少 AITrader.ex5")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    marker = root / "runtime" / "ea_sync.sha256"
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == digest:
        return 0
    targets = installed_ea_targets(appdata)
    updated = 0
    for target in targets:
        if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        temporary = target.with_name(target.name + ".update-tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        updated += 1
    if targets:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(digest, encoding="ascii")
    return updated


def install_stage(root: Path, stage: Path, ea_targets: list[Path] | None = None) -> int:
    files = [(p, p.relative_to(stage / "files")) for p in (stage / "files").rglob("*") if p.is_file()]
    ea_targets = ea_targets or []
    ea_source = stage / "files" / "mql5" / "AITrader.ex5"
    if not ea_source.is_file():
        raise ValueError("更新包缺少已編譯的 AITrader.ex5")
    operations = [(source, root / relative) for source, relative in files]
    operations.extend((ea_source, target) for target in ea_targets)
    backup = stage / "backup"
    existed = []
    created = []
    try:
        for index, (source, target) in enumerate(operations):
            if target.exists():
                saved = backup / str(index)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
                existed.append((saved, target))
            else:
                created.append(target)
        for source, target in operations:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    except Exception:
        for saved, target in existed:
            shutil.copy2(saved, target)
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return len(ea_targets)


def apply_update(root: Path, expected_tag: str, read_url=_read_url) -> int:
    release = latest_release(root, read_url)
    if release["tag"] != expected_tag or version_tuple(release["version"]) <= version_tuple(current_version(root)):
        raise ValueError("發布版本已改變或不是新版；請重新檢查更新")
    (root / "runtime").mkdir(exist_ok=True)
    stage = verified_stage(root, release, read_url)
    targets = installed_ea_targets()
    was_running = _service_running(root)
    if was_running:
        (root / "runtime" / "stop.request").write_text("stop", encoding="ascii")
        deadline = time.monotonic() + 60
        while _service_running(root) and time.monotonic() < deadline:
            time.sleep(0.5)
        if _service_running(root):
            raise ValueError("服務未能在 60 秒內停止；更新未安裝")
    try:
        ea_count = install_stage(root, stage, targets)
    except Exception:
        if was_running:
            _start_service(root)
        raise
    if was_running:
        _start_service(root)
    gui_out = (root / "runtime" / "update.gui.out.log").open("a", encoding="utf-8")
    gui_err = (root / "runtime" / "update.gui.err.log").open("a", encoding="utf-8")
    try:
        gui = subprocess.Popen([sys.executable.replace("python.exe", "pythonw.exe") if os.name == "nt" else sys.executable,
                                "-m", "aitrader.setup_gui"], cwd=root, stdout=gui_out, stderr=gui_err,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        gui_out.close()
        gui_err.close()
    time.sleep(0.5)
    if gui.poll() is not None:
        raise RuntimeError("新版設定視窗啟動失敗；請查看 runtime/update.gui.err.log")
    return ea_count


def _start_service(root: Path) -> None:
    (root / "runtime" / "stop.request").unlink(missing_ok=True)
    out = (root / "runtime" / "service.out.log").open("a", encoding="utf-8")
    err = (root / "runtime" / "service.err.log").open("a", encoding="utf-8")
    try:
        subprocess.Popen([sys.executable, "-m", "aitrader.multi", "run", "--root", str(root)],
                         cwd=root, stdout=out, stderr=err,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        out.close()
        err.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["apply"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    log = root / "runtime" / "update.log"
    try:
        count = apply_update(root, args.tag)
        log.write_text(f"更新完成：{args.tag}；已覆蓋 {count} 個 MT5 EA 資料夾。請在每個 MT5 重新掛載 EA。\n", encoding="utf-8")
    except Exception as exc:
        # URL and secrets are never logged; errors here are deliberately terse.
        log.write_text("更新失敗：" + type(exc).__name__ + "：" + str(exc)[:200] + "\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
