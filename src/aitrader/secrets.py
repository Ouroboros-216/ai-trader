"""Windows current-user DPAPI storage for the setup window.

The encrypted file can be decrypted only under the same Windows user profile.
Environment variables remain supported for command-line deployments.
"""

from __future__ import annotations

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path


ALLOWED_KEYS = {"GEMINI_API_KEY", "OPENAI_API_KEY", "AI_TRADER_TELEGRAM_TOKEN"}


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _api():
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob),
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    return crypt, kernel


def _blob(raw: bytes):
    buffer = ctypes.create_string_buffer(raw)
    return _Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(raw: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for local secret storage")
    source, keepalive = _blob(raw)
    result = _Blob()
    crypt, kernel = _api()
    if not crypt.CryptProtectData(ctypes.byref(source), "AI Trader secrets", None, None, None, 1, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "DPAPI encryption failed")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _unprotect(raw: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is required for local secret storage")
    source, keepalive = _blob(raw)
    result = _Blob()
    crypt, kernel = _api()
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "DPAPI decryption failed; use the same Windows user")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def read_secrets(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        return {}
    data = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
    if not isinstance(data, dict) or any(k not in ALLOWED_KEYS or not isinstance(v, str) for k, v in data.items()):
        raise ValueError("invalid encrypted secret store")
    return data


def save_secrets(path: str | Path, updated: dict[str, str]) -> None:
    path = Path(path)
    existing = read_secrets(path)
    for key, value in updated.items():
        if key not in ALLOWED_KEYS or not isinstance(value, str):
            raise ValueError("unsupported secret")
        if value:
            existing[key] = value
    if not existing:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _protect(json.dumps(existing, ensure_ascii=False).encode("utf-8"))
    fd, name = tempfile.mkstemp(prefix="secrets-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_into_environment(config_path: str | Path) -> None:
    path = Path(config_path).resolve().parent / "secrets.bin"
    # Explicit encrypted settings take precedence over stale User environment entries.
    for key, value in read_secrets(path).items():
        os.environ[key] = value
