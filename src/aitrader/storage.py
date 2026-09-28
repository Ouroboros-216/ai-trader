from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time REAL, kind TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, time REAL, kind TEXT, model TEXT,
          status TEXT, latency REAL, usage TEXT, cost REAL);
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, base_version INTEGER,
          expires REAL, kind TEXT, data TEXT, status TEXT);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, parent TEXT, created REAL,
          expires REAL, version INTEGER, data TEXT, status TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS seen(source TEXT, id TEXT, PRIMARY KEY(source,id));
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dumps(value)))

    def event(self, kind, data):
        with self.db:
            self.db.execute("INSERT INTO events(time,kind,data) VALUES(?,?,?)", (time.time(), kind, dumps(data)))

    def seen(self, source, identifier):
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (source, str(identifier))).rowcount == 0

    def recent(self, count=20):
        rows = self.db.execute("SELECT time,kind,data FROM events ORDER BY id DESC LIMIT ?", (count,))
        return [dict(r) | {"data": json.loads(r["data"])} for r in rows][::-1]

    def memory(self, count=12):
        rows = self.db.execute("SELECT time,kind,data FROM events WHERE kind IN ('decision','execution','conversation','confirmed','uncertain') ORDER BY id DESC LIMIT ?", (count,))
        return [{"time": r["time"], "kind": r["kind"], "summary": r["data"][:1500]} for r in rows][::-1]

    def reserve_call(self, kind, cfg, now):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            backoff = max(self.get("provider_backoff_until", 0),
                          self.get("provider_transient_backoff_until:" + cfg["model"], 0))
            if now < backoff:
                raise ValueError("AI API 暫時等待約 " + str(int(backoff-now)+1) + " 秒後再試")
            last = self.db.execute("SELECT MAX(time) FROM calls").fetchone()[0]
            if last is not None and now - last < cfg["min_interval_seconds"]:
                raise ValueError("API local cooldown active")
            return self.db.execute("INSERT INTO calls(time,kind,model,status) VALUES(?,?,?,'reserved')", (now, kind, cfg["model"])).lastrowid


class ProcessLock:
    """OS lock held for lifetime; process death releases it without stale-PID recovery."""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            self.file.seek(0)
            if self.file.read(1) == b"":
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("another agent already owns this bridge") from None
        return self

    def __exit__(self, *_):
        self.file.close()
