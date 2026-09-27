"""Broker symbol discovery and conservative suffix matching."""

from __future__ import annotations

import re
import time


ALIASES = {
    "黃金": ("XAU", "GOLD"), "白銀": ("XAG", "SILVER"),
    "比特幣": ("BTC",), "以太幣": ("ETH",),
    "歐元": ("EUR",), "英鎊": ("GBP",), "日圓": ("JPY",),
    "原油": ("OIL", "WTI", "BRENT"),
}
TOKEN = re.compile(r"[A-Za-z0-9_.#-]{1,100}")


def canonical(symbol: str) -> str:
    base = re.split(r"[.#_-]", symbol, 1)[0]
    if base == symbol and len(symbol) == 7 and symbol[-1:].lower() == "m" and symbol[:6].isalpha():
        base = symbol[:6]
    return base.upper()


def catalog(agent, snapshot: dict | None = None) -> list[str]:
    """Use the current EA's visible Market Watch catalog; old EA falls back to its snapshot."""
    try:
        raw = agent.bridge.json("catalog.json")
    except (OSError, ValueError):
        raw = None
    if raw is None:
        return list(snapshot["symbols"]) if snapshot else []
    if not isinstance(raw, dict):
        raise ValueError("券商商品清單格式不正確")
    if (str(raw.get("account")) != agent.cfg["account"] or raw.get("server") != agent.cfg["server"] or
        raw.get("magic") != agent.cfg["magic"] or
        raw.get("account_mode", "demo") != agent.cfg.get("account_mode", "demo") or
        raw.get("demo") is not (agent.cfg.get("account_mode", "demo") == "demo") or
        type(raw.get("time")) is not int or abs(time.time() - raw["time"]) > 300):
        raise ValueError("券商商品清單已過期或帳號不符；請檢查新版 EA 是否仍掛載")
    symbols = raw.get("symbols")
    if (not isinstance(symbols, list) or len(symbols) > 10000 or
        any(not isinstance(s, str) or not TOKEN.fullmatch(s) for s in symbols) or
        len(symbols) != len(set(symbols))):
        raise ValueError("券商商品清單格式不正確")
    return [s for s in symbols if s in snapshot["symbols"]] if snapshot else symbols


def relevant(symbols: list[str], instruction: str, active: list[str] = ()) -> list[str]:
    """Filter a large broker catalog by explicit symbols or familiar Chinese asset names."""
    terms = [word.upper() for word in TOKEN.findall(instruction) if len(word) >= 3]
    for chinese, aliases in ALIASES.items():
        if chinese in instruction:
            terms.extend(aliases)
    matching = [s for s in symbols if any(s.upper().startswith(term) or term in s.upper() for term in terms)]
    if matching:
        return list(dict.fromkeys([*matching, *(s for s in active if s in symbols)]))[:300]
    if len(symbols) <= 300:
        return symbols
    raise ValueError("券商商品很多；請在策略中寫明 MT5 商品代碼或商品名稱")


def ambiguous_choice(symbol: str, instruction: str, available: list[str]) -> list[str]:
    group = [s for s in available if canonical(s) == canonical(symbol)]
    if len(group) <= 1 or symbol.lower() in instruction.lower():
        return []
    return group


def account_mapping(symbols: list[str]) -> dict[str, str]:
    groups: dict[str, list[str]] = {}
    for symbol in symbols:
        groups.setdefault(canonical(symbol), []).append(symbol)
    mapping = {symbol: symbol for symbol in symbols}
    for base, choices in groups.items():
        if len(choices) == 1:
            mapping.setdefault(base, choices[0])
    return mapping
