import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

from aitrader.updater import _archive_files, install_stage, installed_ea_targets, latest_release, sync_bundled_ea, verified_stage, version_tuple
import aitrader.setup_gui as setup_gui


def package(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def test_verified_release_installs_code_without_touching_local_state(tmp_path):
    root = tmp_path / "ai-trader"
    (root / "config").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "pyproject.toml").write_text('[project]\nversion="0.8.3"\n', encoding="utf-8")
    (root / "config" / "local.json").write_text("private account", encoding="utf-8")
    (root / "runtime" / "trades.sqlite").write_bytes(b"trades")
    data = package({"ai-trader/pyproject.toml": '[project]\nversion="0.8.4"\n',
                    "ai-trader/src/aitrader/setup_gui.py": "new GUI",
                    "ai-trader/src/aitrader/updater.py": "new updater",
                    "ai-trader/mql5/AITrader.ex5": b"compiled ea",
                    "ai-trader/config/example.json": "{}"})
    digest = hashlib.sha256(data).hexdigest()
    release = {"version": "0.8.4", "zip_name": "AITrader-v0.8.4.zip", "zip_url": "https://example.test/package",
               "sha_url": "https://example.test/checksum"}
    def read_url(url, *_):
        return data if url == release["zip_url"] else f"{digest}  AITrader-v0.8.4.zip\n".encode()
    stage = verified_stage(root, release, read_url)
    install_stage(root, stage)
    assert (root / "pyproject.toml").read_text() == '[project]\nversion="0.8.4"\n'
    assert (root / "config" / "local.json").read_text() == "private account"
    assert (root / "runtime" / "trades.sqlite").read_bytes() == b"trades"


def test_update_overwrites_only_previously_installed_mt5_ea(tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    terminal = appdata / "MetaQuotes" / "Terminal" / "terminal-A"
    installed = terminal / "MQL5" / "Experts" / "AITrader" / "AITrader.ex5"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"old ea")
    (appdata / "MetaQuotes" / "Terminal" / "terminal-B").mkdir()
    root = tmp_path / "app"
    stage = tmp_path / "stage"
    (stage / "files" / "mql5").mkdir(parents=True)
    (stage / "files" / "mql5" / "AITrader.ex5").write_bytes(b"new ea")
    assert installed_ea_targets(appdata) == [installed]
    assert install_stage(root, stage, installed_ea_targets(appdata)) == 1
    assert installed.read_bytes() == b"new ea"
    assert not (appdata / "MetaQuotes" / "Terminal" / "terminal-B" / "MQL5").exists()


def test_old_updater_is_completed_by_new_setup_window(tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    target = appdata / "MetaQuotes" / "Terminal" / "terminal-A" / "MQL5" / "Experts" / "AITrader" / "AITrader.ex5"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old ea")
    root = tmp_path / "app"
    (root / "mql5").mkdir(parents=True)
    (root / "mql5" / "AITrader.ex5").write_bytes(b"new ea")
    assert sync_bundled_ea(root, appdata) == 1
    assert target.read_bytes() == b"new ea"
    assert sync_bundled_ea(root, appdata) == 0


@pytest.mark.parametrize("name", ["ai-trader/../runtime/stop.request", "ai-trader/src\\evil.py",
                                  "ai-trader/config/local.json"])
def test_update_rejects_unsafe_or_private_paths(name):
    data = package({"ai-trader/pyproject.toml": "x", name: "bad"})
    with zipfile.ZipFile(io.BytesIO(data)) as archive, pytest.raises(ValueError):
        _archive_files(archive)


def test_release_must_match_configured_public_repository(tmp_path):
    root = tmp_path
    (root / "config").mkdir()
    (root / "config" / "update.json").write_text('{"repo":"Ouroboros-216/ai-trader"}')
    body = b'{"tag_name":"v0.8.4","assets":[{"name":"AITrader-v0.8.4.zip","browser_download_url":"https://other.test/a"},{"name":"AITrader-v0.8.4.sha256","browser_download_url":"https://other.test/b"}]}'
    with pytest.raises(ValueError, match="倉庫不符"):
        latest_release(root, lambda *_: body)
    assert version_tuple("v0.8.4") > version_tuple("0.8.3")


def test_update_window_stays_open_until_new_window_signals_ready(tmp_path):
    window = object.__new__(setup_gui.SetupWindow)
    events, statuses = [], []
    window.root = SimpleNamespace(after=lambda delay, callback: events.append(callback),
                                  destroy=lambda: events.append("destroyed"))
    window.status = SimpleNamespace(set=statuses.append)
    marker = tmp_path / "update-gui-ready-test"
    process = SimpleNamespace(poll=lambda: None)
    window._watch_update(process, marker)
    assert len(events) == 1 and "destroyed" not in events
    process.poll = lambda: 0
    events.pop(0)()
    assert "destroyed" not in events
    marker.write_text("ready", encoding="ascii")
    events.pop(0)()
    assert events == ["destroyed"] and not marker.exists()


def test_update_failure_keeps_setup_window_open(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_gui, "ROOT", tmp_path)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "update.log").write_text("更新失敗：下載校驗不符", encoding="utf-8")
    window = object.__new__(setup_gui.SetupWindow)
    statuses = []
    window.root = SimpleNamespace(after=lambda *_: pytest.fail("failure must not reschedule"),
                                  destroy=lambda: pytest.fail("failure must not close window"))
    window.status = SimpleNamespace(set=statuses.append)
    window._watch_update(SimpleNamespace(poll=lambda: 1), tmp_path / "runtime" / "update-gui-ready-test")
    assert "下載校驗不符" in statuses[-1]


def test_new_setup_window_writes_ready_signal_after_showing(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_gui, "ROOT", tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    marker = runtime / "update-gui-ready-test"
    monkeypatch.setenv("AI_TRADER_GUI_READY_FILE", str(marker))
    calls = []
    window = object.__new__(setup_gui.SetupWindow)
    window.root = SimpleNamespace(deiconify=lambda: calls.append("show"),
                                  update_idletasks=lambda: calls.append("paint"),
                                  mainloop=lambda: calls.append("loop"))
    window.run()
    assert calls == ["show", "paint", "loop"]
    assert marker.read_text(encoding="ascii") == "ready"
