"""Append-only run log (JSON lines, flushed per line) and an atomically replaced status.json.

A run interrupted by a dropped link or a kill keeps everything logged up to that point.
"""
import json
import os
import time


class RunLog:
    def __init__(self, run_dir):
        self.dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self._fh = open(os.path.join(run_dir, "calls.jsonl"), "a", buffering=1)
        self.status_path = os.path.join(run_dir, "status.json")

    def write(self, **rec):
        rec.setdefault("utc", time.time())
        self._fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        self._fh.flush()

    def status(self, **st):
        st["updated"] = time.time()
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(st, fh, indent=1)
        os.replace(tmp, self.status_path)

    def save(self, name, obj):
        with open(os.path.join(self.dir, name), "w") as fh:
            json.dump(obj, fh, indent=1)

    def close(self):
        self._fh.close()


def read(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]
