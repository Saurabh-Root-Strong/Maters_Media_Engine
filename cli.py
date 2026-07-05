"""Media-Engine v0 CLI.

    python cli.py "Iran-Iraq war"

Give it a topic; it researches, picks an angle, and prints an Instagram,
Twitter, and LinkedIn draft. No posting happens in v0 — this proves the
content quality before any paid API is wired.
"""

from __future__ import annotations

import json
import os
import sys

# Windows consoles default to cp1252 and choke on emoji (⚠ · ✓). Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dotenv import load_dotenv

load_dotenv()

from engine import llm, manifest, orchestrator, publish  # noqa: E402  (after load_dotenv)

_RULE = "=" * 70
_OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
_VERDICT_MARK = {"PASS": "✓ PASS", "REVISE": "~ REVISE", "HOLD": "⚠ HOLD"}


def _progress(msg: str) -> None:
    print(f"  · {msg}", file=sys.stderr, flush=True)


def _render(result: dict) -> None:
    brief, angle, drafts = result["brief"], result["angle"], result["drafts"]

    print(f"\n{_RULE}\nTOPIC: {result['topic']}\n{_RULE}")

    print("\nBRIEF")
    print(f"  {brief['headline_summary']}")
    print(f"  Sentiment: {brief['sentiment']}")
    print(f"  Trending: {', '.join(brief['trending_keywords'][:8])}")
    if brief["sensitivity_flags"]:
        print(f"  ⚠ Sensitivity: {', '.join(brief['sensitivity_flags'])}")

    print(f"\nANGLE  [{angle['angle_type']}]")
    print(f"  {angle['angle']}")
    print(f"  Key message: {angle['key_message']}")

    labels = {"instagram": "INSTAGRAM", "twitter": "TWITTER / X", "linkedin": "LINKEDIN"}
    for key, draft in drafts.items():
        print(f"\n{_RULE}\n{labels.get(key, key.upper())}\n{_RULE}")
        print(draft["caption"])
        tags = " ".join(f"#{h.lstrip('#')}" for h in draft["hashtags"])
        if tags:
            print(f"\n{tags}")
        print(f"\n[chars: {len(draft['caption'])}]")
        if draft.get("image_prompt"):
            print(f"\n  ALT TEXT: {draft['alt_text']}")

    if result.get("image"):
        img = result["image"]
        print(f"\n{_RULE}\nINSTAGRAM IMAGE")
        print(f"  Saved: {os.path.abspath(img['path'])}")
        print(f"  Headline: {img['spec']['headline']}")
    elif result.get("image_error"):
        print(f"\n  ⚠ Image render failed: {result['image_error']}")

    # review / gate
    gate = result["review"]
    print(f"\n{_RULE}\nREVIEW  →  {_VERDICT_MARK.get(gate['recommendation'], gate['recommendation'])}")
    print(f"  {gate['critique_summary']}")
    for key, g in gate["gate"].items():
        print(f"  {labels.get(key, key):<12} {_VERDICT_MARK.get(g['verdict'], g['verdict'])}")
        for issue in [i["msg"] for i in g["limit_issues"]] + g["editorial_issues"]:
            print(f"      - {issue}")

    print(f"\n{_RULE}")
    print("Sources:")
    for src in brief["sources"]:
        print(f"  - {src}")


def _approve(result: dict) -> None:
    gate = result["review"]
    print(f"\n{_RULE}")
    if gate["requires_human"]:
        print("⚠ This run REQUIRES human sign-off (sensitive topic or HOLD verdict).")
    if not sys.stdin.isatty():
        print("Non-interactive shell — not approving. Re-run in a terminal to approve.")
        return
    ans = input("Approve all drafts for publishing? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("Held. Nothing approved, nothing written.")
        return

    path, frozen = manifest.write(result, _OUT_DIR)
    print(f"Approved → manifest: {os.path.abspath(path)}")
    _publish(frozen)


def _publish(frozen: dict) -> None:
    # v3 is dry-run: show the exact call each platform would fire, no posting.
    print(f"\n{_RULE}\nPUBLISH (dry-run — nothing is sent)")
    for r in publish.run(frozen, live=False):
        plat = r["platform"]
        if r["status"] == "dry-run":
            req = r["request"]
            url = req.get("url") or req.get("steps", [{}])[0].get("url", "?")
            print(f"\n  {plat}: would {req['method']} {url}")
            body = req.get("body") or req.get("steps")
            print(f"    body: {json.dumps(body, ensure_ascii=False)[:200]}")
            for n in req.get("notes", []):
                print(f"    · {n}")
        else:
            print(f"\n  {plat}: {r['status']} — {r.get('reason', '')}")
    print("\n  To go live later: add each platform's credentials to .env and")
    print("  wire its publisher._send(). Nothing posts until then.")


_ACTION_MARK = {
    "PUBLISH": "→ PUBLISH", "DRY_RUN": "· dry-run", "HOLD_FOR_HUMAN": "⚠ HOLD",
    "BLOCK": "✗ BLOCK", "SKIP": "– skip",
}


def _run_auto(topic: str) -> None:
    from engine import autopost, policy

    config = policy.Config.from_env()
    print(
        f"  auto-publish={config.auto_publish}  live={config.live}  "
        f"platforms={','.join(sorted(config.enabled))}",
        file=sys.stderr,
    )
    auto = autopost.run(topic, config, on_progress=_progress)
    _render(auto["result"])

    print(f"\n{_RULE}\nAUTO-POLICY DECISIONS")
    for platform, d in auto["decisions"].items():
        mark = _ACTION_MARK.get(d["action"], d["action"])
        print(f"  {platform:<12} {mark:<12} {d['reason']}")
        out = auto["outcomes"].get(platform, {})
        if out.get("status") == "posted":
            print(f"      posted: {out.get('url', out.get('id', 'ok'))}")
        elif out.get("status") == "error":
            print(f"      error: {out['error']}")

    if auto["held"]:
        print(f"\n  {len(auto['held'])} platform(s) held for a human: {', '.join(auto['held'])}")
        _approve(auto["result"])  # reuse the manual y/N + dry-run publish for held items


def _run_queue(path: str) -> None:
    """Batch/scheduler: run every topic in a file through the auto-policy.

    Non-interactive — held items are written to output/<slug>.held.json for
    later human review, never auto-fired.
    """
    from engine import autopost, policy

    config = policy.Config.from_env()
    with open(path, encoding="utf-8") as f:
        topics = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    print(f"Queue: {len(topics)} topic(s) | auto={config.auto_publish} live={config.live}")

    for topic in topics:
        print(f"\n{_RULE}\n▶ {topic}")
        try:
            auto = autopost.run(topic, config, on_progress=_progress)
        except Exception as exc:  # noqa: BLE001 — one bad topic must not kill the queue
            print(f"  ✗ failed: {exc}")
            continue
        for platform, d in auto["decisions"].items():
            print(f"  {platform:<12} {_ACTION_MARK.get(d['action'], d['action'])}")
        if auto["held"]:
            hpath, _ = manifest.write(auto["result"], _OUT_DIR, "auto-policy", state="held")
            print(f"  held -> {os.path.abspath(hpath)} (review + approve later)")


def main() -> int:
    argv = sys.argv[1:]
    auto_mode = "--auto" in argv
    queue_path = None
    if "--queue" in argv:
        i = argv.index("--queue")
        queue_path = argv[i + 1] if i + 1 < len(argv) else None
        argv = argv[:i] + argv[i + 2 :]
    args = [a for a in argv if a != "--auto"]

    if not llm.has_api_key():
        print(
            f"{llm.key_var()} not set. Copy .env.example to .env and add your key.",
            file=sys.stderr,
        )
        return 1

    if queue_path:
        _run_queue(queue_path)
        return 0

    if not args or not args[0].strip():
        print('Usage: python cli.py [--auto] "your topic"', file=sys.stderr)
        print('       python cli.py --queue topics.txt', file=sys.stderr)
        return 2

    topic = " ".join(args).strip()
    if auto_mode:
        _run_auto(topic)
    else:
        result = orchestrator.run(topic, on_progress=_progress)
        _render(result)
        _approve(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
