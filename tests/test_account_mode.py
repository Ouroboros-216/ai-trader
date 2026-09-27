import json
import time
from pathlib import Path

import pytest

from aitrader.accounts import new_profile_config, write_mt5_index
from aitrader.bridge import atomic_write
from aitrader.contracts import MarketSnapshot
from aitrader.service import Agent, load_config
from aitrader.setup_gui import save_form


def test_existing_profile_defaults_to_demo_and_new_profile_does_not_inherit_real(tmp_path):
    template = json.loads((Path(__file__).parents[1] / "config" / "example.json").read_text())
    template.update(account="101", server="TEST-Real")
    template.pop("account_mode")
    template.pop("live_enabled")
    path = tmp_path / "config" / "local.json"
    path.parent.mkdir()
    path.write_text(json.dumps(template))
    loaded = load_config(path)
    assert loaded["account_mode"] == "demo" and loaded["live_enabled"] is False
    _, _, second = new_profile_config(tmp_path, template | {"account_mode": "real", "live_enabled": True}, "202", "OTHER-Demo")
    assert second["account_mode"] == "demo" and second["live_enabled"] is False


def test_live_save_and_index_agree_on_mode(tmp_path):
    template = json.loads((Path(__file__).parents[1] / "config" / "example.json").read_text())
    common = tmp_path / "MetaQuotes" / "Terminal" / "Common" / "Files"
    template.update(bridge_dir=str(common / "AITrader" / "profile"), database=str(tmp_path / "agent.sqlite"))
    config = tmp_path / "config"
    config.mkdir()
    (config / "example.json").write_text(json.dumps(template))
    result = save_form(config / "local.json", "101", "TEST-Real", "gemini-test", "", False, False,
                       "", "", account_mode="real", live_enabled=True)
    assert result["account_mode"] == "real" and result["live_enabled"] is True
    write_mt5_index({"account": load_config(config / "local.json")})
    assert (common / "AITrader" / "accounts.txt").read_text().strip().endswith("|real|1")
    assert (common / "AITrader" / "profile" / "binding.csv").read_text().strip().endswith(",real,1")


def test_snapshot_must_match_selected_account_mode(snapshot):
    real = snapshot | {"demo": False, "account_mode": "real"}
    assert MarketSnapshot.parse(real, "12345", "TEST-Demo", 26092751, time.time(), expected_mode="real").data == real
    with pytest.raises(ValueError):
        MarketSnapshot.parse(snapshot, "12345", "TEST-Demo", 26092751, time.time(), expected_mode="real")
    with pytest.raises(ValueError):
        MarketSnapshot.parse(real, "12345", "TEST-Demo", 26092751, time.time(), expected_mode="demo")


def test_real_agent_uses_real_snapshot_and_existing_activation_confirmation(agent, snapshot):
    cfg = agent.cfg | {"account_mode": "real", "live_enabled": True,
                       "database": str(agent.bridge.root / "real.sqlite")}
    real = Agent(cfg, provider=object())
    try:
        raw = snapshot | {"demo": False, "account_mode": "real"}
        atomic_write(real.bridge.root / "snapshot.json", json.dumps(raw))
        real.store.set("policy", agent.store.get("policy"))
        answer = real.handle("啟動")
        assert "實盤" in answer and "確認" in answer
        identifier = answer.split("確認 ", 1)[1]
        assert "實盤自動交易已啟用" in real.confirm(identifier)
    finally:
        real.close()
