import csv
import io
import json
import os
import uuid
from pathlib import Path

from .storage import dumps


def atomic_write(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Bridge:
    def __init__(self, path):
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)

    def json(self, name):
        path = self.root / name
        if path.stat().st_size > 5_000_000:
            raise ValueError("bridge file too large")
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def csv(self, name, values):
        # Wire fields must not require CSV quoting: MQL parser splits a single line.
        cells = [str(v) for v in values]
        if any(any(c in v for c in ',\r\n"') for v in cells):
            raise ValueError("invalid wire field")
        atomic_write(self.root / name, ",".join(cells) + "\n")

    def lines(self, name):
        path = self.root / name
        if not path.exists():
            return []
        # Ignore incomplete trailing writes, retry them on the next cycle.
        with path.open(encoding="utf-8-sig") as f:
            return [json.loads(line) for line in f if line.endswith("\n") and line.strip()]

    def tail(self, name, offset=0, limit=500):
        path = self.root / name
        if not path.exists():
            if offset:
                raise ValueError("audit log disappeared")
            return [], 0
        if path.stat().st_size < offset:
            raise ValueError("audit log truncated")
        records = []
        with path.open("rb") as f:
            f.seek(offset)
            for _ in range(limit):
                raw = f.readline()
                if not raw.endswith(b"\n"):
                    break
                records.append(json.loads(raw.decode("utf-8-sig")))
                offset = f.tell()
        return records, offset

    def status(self, data):
        atomic_write(self.root / "status.json", dumps(data))
        atomic_write(self.root / "panel.txt", "\n".join(str(data.get(k, "")) for k in ("headline", "strategy", "api", "chat", "latest", "pending")))
