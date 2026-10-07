"""Run one trend cycle without the dashboard:

    python -m engine.trends

Collects from every source, rescoring the board, and takes the periodic digest
if one is due. Point Windows Task Scheduler (or cron) at this to keep updates
coming while the dashboard is closed.
"""

from __future__ import annotations

import sys

from . import service


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    summary = service.collect()
    for source, s in summary.items():
        print(f"{source:8} {'ok' if s['ok'] else 'FAILED':6} {s['items']:>4} items  {s['error']}")
    board = service.compute(write=True)
    print("board:", ", ".join(f"{k.lower()} {v}" for k, v in board["counts"].items()))
    update = service.maybe_update()
    if update:
        n = {c: sum(1 for t in update["topics"] if t["cls"] == c and t["niche"])
             for c in ("TRENDING", "SAME", "FADING")}
        print(f"update #{update['id']}: trending {n['TRENDING']}, same {n['SAME']}, fading {n['FADING']}, "
              f"dropped off {len(update['dropped'])}")
    else:
        print("update: not due yet")
    return 0 if any(s["ok"] for s in summary.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
