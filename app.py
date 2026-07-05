"""Media-Engine dashboard — a local web UI.

    python app.py     ->  http://127.0.0.1:5000

Enter a topic, see the three drafts + Instagram image + review verdict, and
post per platform. The per-platform button state and the server-side publish
enforcement both follow the same safety policy: sensitive topics and non-PASS
drafts cannot post without an explicit human confirm (and REVISE/BLOCK not at
all). Publishing is dry-run until MEDIA_ENGINE_LIVE=true + credentials.
"""

from __future__ import annotations

import os
import uuid

from dotenv import load_dotenv

load_dotenv()

import time  # noqa: E402

from flask import Flask, jsonify, render_template, request, send_from_directory  # noqa: E402

from engine import llm, manifest, memory, orchestrator, policy  # noqa: E402
from engine.publishers import BY_KEY  # noqa: E402
from engine.scheduler import scheduler  # noqa: E402

app = Flask(__name__)
_OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# In-memory store of runs (local single-user). run_id -> {"frozen": ..., "review": ...}
_RUNS: dict[str, dict] = {}

scheduler.start()  # background thread that fires due scheduled posts


def _enforce(run: dict, platform: str, confirm: bool):
    """Shared policy gate for publish + schedule. Returns an error response or None."""
    if not run:
        return jsonify({"error": "Run not found — regenerate."}), 404
    if platform not in BY_KEY:
        return jsonify({"error": f"Unknown platform {platform}."}), 400
    gate = run["review"]["gate"].get(platform, {})
    verdict = gate.get("verdict", "PASS")
    sensitive = run["review"].get("sensitive", False)
    if verdict == "REVISE":
        return jsonify({"platform": platform, "status": "blocked",
                        "reason": "failed review — cannot post"}), 200
    if (sensitive or verdict == "HOLD") and not confirm:
        return jsonify({"platform": platform, "status": "needs_confirm",
                        "reason": "sensitive/HOLD — explicit confirmation required"}), 200
    return None


def _creds_map() -> dict[str, bool]:
    return {k: (p.creds() is not None) for k, p in BY_KEY.items()}


def _postability(review: dict) -> dict:
    """Per-platform UI state derived from the gate + credentials."""
    sensitive = review.get("sensitive", False)
    creds = _creds_map()
    out: dict[str, dict] = {}
    for p, g in review.get("gate", {}).items():
        verdict = g.get("verdict", "PASS")
        if verdict == "REVISE":
            state = "blocked"
        elif sensitive or verdict == "HOLD":
            state = "confirm"
        else:
            state = "ready"
        issues = [i["msg"] for i in g.get("limit_issues", [])] + g.get("editorial_issues", [])
        out[p] = {"state": state, "verdict": verdict, "creds": creds.get(p, False),
                  "issues": issues}
    return out


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    from engine import imagegen
    c = policy.Config.from_env()
    return jsonify({"web_search": llm.web_search_enabled(), "image_backend": imagegen.backend(),
                    "image_templates": imagegen.templates(),
                    "live": c.live, "auto": c.auto_publish})


@app.get("/output/<path:name>")
def output_file(name: str):
    return send_from_directory(_OUT_DIR, name)


@app.post("/api/generate")
def api_generate():
    body = request.json or {}
    topic = body.get("topic", "").strip()
    # .get(default) — not `or` — so an explicit [] means "none" (400), not "all".
    selected = body.get("platforms", ["twitter", "instagram", "linkedin"])
    selected = [p for p in selected if p in BY_KEY]
    if not topic:
        return jsonify({"error": "Enter a topic."}), 400
    if not selected:
        return jsonify({"error": "Pick at least one platform."}), 400
    if not llm.has_api_key():
        return jsonify({"error": f"{llm.key_var()} not set — add it to .env and restart."}), 400

    web_search = body.get("web_search")     # None -> env default; bool -> override
    image_backend = body.get("image_backend")  # None / "template" / "openai"
    image_template = body.get("image_template")  # None / "auto" / a template id
    try:
        result = orchestrator.run(topic, platforms_selected=selected,
                                  use_search=web_search, image_backend=image_backend,
                                  image_template=image_template)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Generation failed: {exc}"}), 500

    memory.record_topic(topic)  # history: avoid repeats + inform trending
    run_id = uuid.uuid4().hex
    frozen = manifest.build(result, approved_by="dashboard")
    _RUNS[run_id] = {"frozen": frozen, "review": result["review"]}
    while len(_RUNS) > 50:  # bound run store — drop the oldest run
        del _RUNS[next(iter(_RUNS))]

    image = result.get("image") or {}
    image_url = f"/output/{os.path.basename(image['path'])}" if image.get("path") else None
    config = policy.Config.from_env()

    return jsonify({
        "run_id": run_id,
        "topic": topic,
        "brief": {
            "headline_summary": result["brief"]["headline_summary"],
            "sentiment": result["brief"]["sentiment"],
            "trending_keywords": result["brief"]["trending_keywords"][:8],
            "sensitivity_flags": result["brief"]["sensitivity_flags"],
            "sources": result["brief"]["sources"],
        },
        "angle": result["angle"],
        "drafts": result["drafts"],
        "image_url": image_url,
        "image_error": result.get("image_error"),
        "image_platforms": result.get("image_platforms", []),
        "review": {
            "recommendation": result["review"]["recommendation"],
            "summary": result["review"]["critique_summary"],
            "sensitive": result["review"]["sensitive"],
        },
        "platforms": _postability(result["review"]),
        "mode": {"live": config.live, "auto": config.auto_publish},
    })


@app.post("/api/publish")
def api_publish():
    body = request.json or {}
    run_id = body.get("run_id")
    platform = body.get("platform")
    confirm = bool(body.get("confirm"))

    run = _RUNS.get(run_id)
    err = _enforce(run, platform, confirm)  # never trust the client's button state
    if err:
        return err

    live = policy.Config.from_env().live
    return jsonify(BY_KEY[platform].publish(run["frozen"], live=live)), 200


@app.post("/api/trending")
def api_trending():
    if not llm.has_api_key():
        return jsonify({"error": f"{llm.key_var()} not set."}), 400
    web_search = (request.json or {}).get("web_search")
    try:
        from engine import trending
        return jsonify({"topics": trending.suggest(6, use_search=web_search)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Trending fetch failed: {exc}"}), 500


@app.post("/api/schedule")
def api_schedule():
    body = request.json or {}
    run_id = body.get("run_id")
    platform = body.get("platform")
    confirm = bool(body.get("confirm"))
    when_epoch = body.get("when_epoch")
    when_label = body.get("when_label", "")

    run = _RUNS.get(run_id)
    err = _enforce(run, platform, confirm)
    if err:
        return err
    try:
        when_epoch = float(when_epoch)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid schedule time."}), 400
    if when_epoch <= time.time() + 5:
        return jsonify({"error": "Schedule time must be in the future."}), 400

    live = policy.Config.from_env().live
    job = scheduler.add(platform, when_epoch, when_label, run["frozen"], live)
    return jsonify({"status": "scheduled", "job": job}), 200


@app.get("/api/scheduled")
def api_scheduled():
    return jsonify({"jobs": scheduler.list()})


@app.post("/api/scheduled/cancel")
def api_scheduled_cancel():
    jid = (request.json or {}).get("id")
    return jsonify({"cancelled": scheduler.cancel(jid)})


if __name__ == "__main__":
    import threading
    import webbrowser

    url = "http://127.0.0.1:5000"
    # Open the browser shortly after the server starts listening.
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Media-Engine dashboard -> {url}")
    app.run(host="127.0.0.1", port=5000, debug=False)
