import hashlib
import io
import zipfile

import pytest

from aitrader.updater import _archive_files, install_stage, latest_release, verified_stage, version_tuple


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
