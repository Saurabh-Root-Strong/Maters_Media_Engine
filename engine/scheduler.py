"""Scheduled publishing.

A tiny persistent job runner: schedule a platform post for a future time; a
background thread fires due jobs via the normal publisher (dry-run or live per
the flag captured when the job was created). Jobs are written to disk so they
survive an app restart *while the app is running again* — the dev server only
ticks while the process is up (real cron is a later milestone).

Deliberately simple and fail-soft: one bad job never stops the loop.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime

from .publishers import BY_KEY

_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "scheduled")


def _public(job: dict) -> dict:
    """Job view for the UI — drops the big frozen manifest."""
    return {k: job[k] for k in ("id", "platform", "topic", "when_epoch",
                                "when_label", "live", "status", "detail") if k in job}


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # --- persistence ---
    def _path(self, jid: str) -> str:
        return os.path.join(_DIR, f"{jid}.json")

    def _save(self, job: dict) -> None:
        os.makedirs(_DIR, exist_ok=True)
        with open(self._path(job["id"]), "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)

    def load(self) -> None:
        if not os.path.isdir(_DIR):
            return
        for name in os.listdir(_DIR):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
                        job = json.load(f)
                    self._jobs[job["id"]] = job
                except Exception:  # noqa: BLE001 — skip a corrupt job file
                    continue

    # --- api ---
    def add(self, platform: str, when_epoch: float, when_label: str,
            frozen: dict, live: bool) -> dict:
        job = {
            "id": uuid.uuid4().hex[:12],
            "platform": platform,
            "topic": frozen.get("topic", ""),
            "when_epoch": when_epoch,
            "when_label": when_label,
            "live": live,
            "status": "pending",
            "detail": "",
            "frozen": frozen,
        }
        with self._lock:
            self._jobs[job["id"]] = job
            self._save(job)
        return _public(job)

    def list(self) -> list[dict]:
        with self._lock:
            jobs = [_public(j) for j in self._jobs.values()
                    if j["status"] in ("pending", "error")]
        return sorted(jobs, key=lambda j: j["when_epoch"])

    def cancel(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job or job["status"] != "pending":
                return False
            job["status"] = "cancelled"
            self._save(job)
        return True

    # --- background firing ---
    def _fire(self, job: dict) -> None:
        pub = BY_KEY.get(job["platform"])
        if not pub:
            job["status"], job["detail"] = "error", "unknown platform"
            return
        try:
            res = pub.publish(job["frozen"], live=job["live"])
            job["status"] = "posted" if res.get("status") in ("posted", "dry-run") else "error"
            job["detail"] = res.get("url") or res.get("reason") or res.get("status", "")
        except Exception as exc:  # noqa: BLE001
            job["status"], job["detail"] = "error", str(exc)

    def _tick(self) -> None:
        now = time.time()
        with self._lock:
            due = [j for j in self._jobs.values()
                   if j["status"] == "pending" and j["when_epoch"] <= now]
            for job in due:
                job["status"] = "firing"  # claim it so a slow fire can't double-run
        for job in due:
            self._fire(job)
            with self._lock:
                self._save(job)

    def start(self) -> None:
        if self._thread:
            return
        self.load()

        def loop() -> None:
            while True:
                try:
                    self._tick()
                except Exception:  # noqa: BLE001 — never let the loop die
                    pass
                time.sleep(10)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()


scheduler = Scheduler()
