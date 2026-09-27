import json
import os
from types import SimpleNamespace

import pytest

from aitrader.provider import build_provider
from aitrader.secrets import load_into_environment, read_secrets
from aitrader.setup_gui import save_form, valid_ea
import aitrader.setup_gui as setup_gui
from aitrader.accounts import profile_id, save_registry


def test_setup_writes_encrypted_secrets_and_loadable_ea_preset(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AI_TRADER_TELEGRAM_TOKEN", raising=False)
    template = {"account": "", "server": "", "magic": 26092751,
                "bridge_dir": str(tmp_path / "MetaQuotes/Terminal/Common/Files/AITrader/demo-1"), "database": "agent.sqlite",
                "provider": {"kind": "gemini", "model": "", "enabled": False},
                "telegram": {"enabled": False, "user_id": 0, "chat_id": 0}}
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "example.json").write_text(json.dumps(template), encoding="utf8")
    config_path = config_dir / "local.json"
    result = save_form(config_path, "12345", "TEST-Demo", "gemini-test", "777",
                       True, True, "a-private-gemini-key", "123:private-bot-token", "XAUUSD.a,EURUSD.a", "7,0")
    assert result["provider"]["enabled"] is True and result["telegram"]["enabled"] is True
    assert result["ea"]["symbols"] == "XAUUSD.a,EURUSD.a"
    assert "a-private-gemini-key" not in config_path.read_text(encoding="utf8")
    preset = (config_dir / "AITrader.generated.set").read_text(encoding="utf8")
    assert "InpDemoLogin=12345" in preset and "InpDemoServer=TEST-Demo" in preset
    assert "InpCommissionRoundTurn=7,0" in preset
    assert (tmp_path / "MetaQuotes/Terminal/Common/Files/AITrader/demo-1/binding.csv").read_text(encoding="utf8").strip() == "1,12345,TEST-Demo,26092751,demo,0"
    assert "private" not in preset
    encrypted = (config_dir / "secrets.bin").read_bytes()
    assert b"a-private-gemini-key" not in encrypted and b"private-bot-token" not in encrypted
    assert read_secrets(config_dir / "secrets.bin")["GEMINI_API_KEY"] == "a-private-gemini-key"
    load_into_environment(config_path)
    assert os.environ["AI_TRADER_TELEGRAM_TOKEN"] == "123:private-bot-token"


def test_renewed_package_does_not_need_telegram_to_save_demo_config(tmp_path):
    config = {"account": "12345", "server": "TEST-Demo", "magic": 26092751,
              "bridge_dir": str(tmp_path / "MetaQuotes/Terminal/Common/Files/AITrader/demo-1"),
              "provider": {"kind": "gemini", "model": "old", "enabled": False},
              "telegram": {"enabled": False, "user_id": 0, "chat_id": 0}}
    path = tmp_path / "local.json"
    path.write_text(json.dumps(config), encoding="utf8")
    result = save_form(path, "12345", "TEST-Demo", "new-model", "", False, False, "", "")
    assert result["account"] == "12345" and result["provider"]["model"] == "new-model"
    assert result["telegram"]["enabled"] is False


@pytest.mark.parametrize("symbols,commissions", [("XAUUSD,EURUSD,GBPUSD", "7,0"), ("XAUUSD,XAUUSD", "7,7"), ("XAUUSD", "-2"), ("XAUUSD;EURUSD", "7")])
def test_bad_broker_cost_mapping_blocked(symbols, commissions):
    with pytest.raises(ValueError):
        valid_ea(symbols, commissions)


def test_one_commission_value_is_shared():
    assert valid_ea("XAUUSD,EURUSD,GBPUSD", "7") == ("XAUUSD,EURUSD,GBPUSD", "7")
    assert valid_ea("AUTO", "-1") == ("AUTO", "-1")
    with pytest.raises(ValueError):
        valid_ea("AUTO", "7,0")


def test_provider_factory_rejects_uninstalled_provider():
    with pytest.raises(ValueError):
        build_provider({"kind": "uninstalled"}, object())


def test_openai_selection_keeps_gemini_key_separate(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = tmp_path / "config"
    root.mkdir()
    template = json.loads((setup_gui.ROOT / "config" / "example.json").read_text(encoding="utf-8"))
    template["bridge_dir"] = str(tmp_path / "MetaQuotes" / "Terminal" / "Common" / "Files" / "AITrader" / "demo-1")
    (root / "example.json").write_text(json.dumps(template), encoding="utf-8")
    path = root / "local.json"
    first = save_form(path, "12345", "TEST-Demo", "gemini-test", "", True, False,
                      "gemini-secret", "", secrets_path=root / "secrets.bin")
    second = save_form(path, "12345", "TEST-Demo", "gpt-test", "", True, False,
                       "openai-secret", "", secrets_path=root / "secrets.bin", provider_kind="openai")
    assert first["provider"]["kind"] == "gemini"
    assert second["provider"]["kind"] == "openai"
    assert second["provider"]["api_key_env"] == "OPENAI_API_KEY"
    assert "openai-secret" not in path.read_text(encoding="utf-8")
    secrets = read_secrets(root / "secrets.bin")
    assert secrets["GEMINI_API_KEY"] == "gemini-secret"
    assert secrets["OPENAI_API_KEY"] == "openai-secret"


def test_start_service_loads_all_profiles_without_selected_ea(tmp_path, monkeypatch):
    bridge_a, bridge_b = tmp_path / "bridge-a", tmp_path / "bridge-b"
    profiles = {"a": {"provider": {"enabled": True}, "bridge_dir": str(bridge_a)},
                "b": {"provider": {"enabled": True}, "bridge_dir": str(bridge_b)}}
    monkeypatch.setattr(setup_gui, "ROOT", tmp_path)
    monkeypatch.setattr(setup_gui, "load_profiles", lambda _: ({}, profiles))
    launches = []
    monkeypatch.setattr(setup_gui.subprocess, "Popen", lambda args, **kwargs: launches.append(args) or SimpleNamespace(pid=123))
    monkeypatch.setattr(setup_gui.messagebox, "showerror", lambda *args: pytest.fail(str(args)))
    window = object.__new__(setup_gui.SetupWindow)
    window.save = lambda: True
    window.check_mt5 = lambda: pytest.fail("start_service must not require the selected account's EA")
    messages = []
    window.status = SimpleNamespace(set=messages.append)
    window.start_service()
    assert len(launches) == 1 and launches[0][2] == "aitrader.multi"
    assert "2 個帳號" in messages[-1]


def test_account_dropdown_shows_broker_and_selects_original_profile(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    (config_dir / "accounts").mkdir(parents=True)
    template = json.loads((setup_gui.ROOT / "config" / "example.json").read_text(encoding="utf-8"))
    first = template | {"account": "53070196", "server": "ICMarketsSC-Demo"}
    second = template | {"account": "26091375", "server": "VantageMarkets-Demo"}
    a, b = profile_id(first["account"], first["server"]), profile_id(second["account"], second["server"])
    (config_dir / "local.json").write_text(json.dumps(first))
    (config_dir / "accounts" / (b + ".json")).write_text(json.dumps(second))
    registry = {"version": 1, "profiles": {a: "local.json", b: "accounts/" + b + ".json"}, "shared_profile": a}
    save_registry(tmp_path, registry)
    monkeypatch.setattr(setup_gui, "ROOT", tmp_path)
    labels = setup_gui.account_labels(tmp_path, registry)
    assert list(labels.values()) == ["ICMarketsSC-Demo｜53070196", "VantageMarkets-Demo｜26091375"]
    window = object.__new__(setup_gui.SetupWindow)
    window.registry, window.account_labels = registry, labels
    window.account_choice = SimpleNamespace(get=lambda: labels[b])
    selected = {}
    window.vars = {name: SimpleNamespace(set=lambda value, key=name: selected.__setitem__(key, value))
                   for name in ("account", "server", "symbols", "commissions", "model", "provider", "account_mode", "user_id")}
    window.status = SimpleNamespace(set=lambda value: selected.__setitem__("status", value))
    window.model_drafts = {}
    window.provider_test_button = SimpleNamespace(configure=lambda **kwargs: None)
    window._choose_account()
    assert window.selected_id == b
    assert selected["account"] == "26091375" and selected["server"] == "VantageMarkets-Demo"
