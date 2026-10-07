"""Media-Engine dashboard — a local web UI.

    python app.py     ->  http://127.0.0.1:5000

Enter a topic, see the three drafts + Instagram image + review verdict, and
post per platform. The per-platform button state and the server-side publish
enforcement both follow the same safety policy: sensitive topics and non-PASS
drafts cannot post without an explicit human confirm (and REVISE/BLOCK not at
all). Publishing is dry-run until MEDIA_ENGINE_LIVE=true + credentials.
"""

from __future__ import annotations

import math
import os
import uuid

from dotenv import load_dotenv

load_dotenv()

import time  # noqa: E402

from flask import Flask, abort, jsonify, render_template, request, send_from_directory  # noqa: E402

from engine import analytics, llm, manifest, orchestrator, policy  # noqa: E402
from engine.publishers import BY_KEY  # noqa: E402
from engine.scheduler import scheduler  # noqa: E402
from engine.trends import service as trends  # noqa: E402

app = Flask(__name__)
_OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

# In-memory store of runs (local single-user). run_id -> {"frozen": ..., "review": ...}
_RUNS: dict[str, dict] = {}

# Background thread that fires due scheduled posts. The test suite switches it
# off so importing `app` never loads or fires the real jobs on disk.
if os.environ.get("MEDIA_ENGINE_SCHEDULER", "on").strip().lower() != "off":
    scheduler.start()

# Background trend collector (free public feeds, every config/trends.yaml
# interval). Same switch-off for tests: they must never hit the network.
if os.environ.get("MEDIA_ENGINE_TRENDS", "on").strip().lower() != "off":
    trends.service.start()


def _log_post(platform: str, run: dict, result: dict) -> None:
    """Record a publish outcome in analytics.db. Never breaks the publish."""
    try:
        analytics.record_post(platform, run["frozen"], result, run.get("meta"))
    except Exception:  # noqa: BLE001
        pass


def _body() -> dict:
    """Parse the JSON body defensively — bad/missing JSON is {} not a 415."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _topic(value) -> str:
    """A usable topic or ''. Only real text counts: a JSON object or number
    stringified into "{'a': 1}" would otherwise be researched and posted about,
    and a single letter gives the model nothing but room to invent."""
    if not isinstance(value, str):
        return ""
    topic = " ".join(value.split())[:300]
    return topic if sum(ch.isalnum() for ch in topic) >= 3 else ""


def _enforce(run: dict, platform: str, confirm: bool):
    """Shared policy gate for publish + schedule. Returns an error response or None."""
    if not run:
        return jsonify({"error": "Run not found — regenerate."}), 404
    if not isinstance(platform, str) or platform not in BY_KEY:
        return jsonify({"error": f"Unknown platform {platform}."}), 400
    if platform not in run["frozen"].get("drafts", {}):
        return jsonify({"error": f"No {platform} draft in this run."}), 400
    gate = run["review"]["gate"].get(platform, {})
    # Missing verdict = never reviewed -> HOLD. Only an explicit PASS posts
    # without a human confirm.
    verdict = gate.get("verdict", "HOLD")
    sensitive = run["review"].get("sensitive", False)
    if verdict == "REVISE":
        return jsonify({"platform": platform, "status": "blocked",
                        "reason": "failed review — cannot post"}), 200
    if (sensitive or verdict != "PASS") and not confirm:
        return jsonify({"platform": platform, "status": "needs_confirm",
                        "reason": "sensitive/HOLD — explicit confirmation required"}), 200
    return None


def _creds_map() -> dict[str, bool]:
    return {k: (p.creds() is not None) for k, p in BY_KEY.items()}


def _postability(review: dict, drafts: dict | None = None, sources: list | None = None) -> dict:
    """Per-platform UI state derived from the gate + credentials."""
    from engine import algo, drafters
    from engine import review as review_mod

    sensitive = review.get("sensitive", False)
    creds = _creds_map()
    specs = drafters._load_platforms()
    drafts = drafts or {}
    out: dict[str, dict] = {}
    for p, g in review.get("gate", {}).items():
        verdict = g.get("verdict", "HOLD")
        if verdict == "REVISE":
            state = "blocked"
        elif sensitive or verdict != "PASS":
            state = "confirm"
        else:
            state = "ready"
        issues = [i["msg"] for i in g.get("limit_issues", [])] + g.get("editorial_issues", [])
        out[p] = {"state": state, "verdict": verdict, "creds": creds.get(p, False),
                  "issues": issues}
        if p in drafts:
            # The number the gate actually judges: caption + hashtags, emoji
            # weighted where the platform does — not the bare caption length.
            spec = specs.get(p, {})
            out[p]["chars"] = review_mod.char_count(review_mod.post_text(drafts[p]), spec)
            out[p]["limit"] = spec.get("caption_max_chars")
            # How well the draft is shaped for the platform's ranking. Advisory
            # only — it never changes `state`; the review gate decides that.
            out[p]["fit"] = algo.fit(p, drafts[p], sources)
    return out


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def api_config():
    from engine import drafters, imagegen
    c = policy.Config.from_env()
    return jsonify({"web_search": llm.web_search_enabled(), "image_backend": imagegen.backend(),
                    "image_templates": imagegen.templates(),
                    "platform_templates": drafters.platform_templates(),
                    "live": c.live, "auto": c.auto_publish,
                    "writers": [f"{p}:{m}" for p, m in llm.writers()]})


@app.get("/output/<path:name>")
def output_file(name: str):
    # Rendered images only. output/ also holds scheduled jobs, manifests and
    # the topic history — none of which the browser has any business reading.
    if "/" in name or "\\" in name or not name.lower().endswith(".png"):
        abort(404)
    return send_from_directory(_OUT_DIR, name)


@app.post("/api/generate")
def api_generate():
    body = _body()
    topic = _topic(body.get("topic"))
    # .get(default) — not `or` — so an explicit [] means "none" (400), not "all".
    selected = body.get("platforms", ["twitter", "instagram", "linkedin"])
    if not isinstance(selected, list):  # null / string / number -> "none", not a 500
        selected = []
    selected = [p for p in selected if isinstance(p, str) and p in BY_KEY]
    if not topic:
        return jsonify({"error": "Enter a topic — a few words naming the story."}), 400
    if not selected:
        return jsonify({"error": "Pick at least one platform."}), 400
    if not llm.has_api_key():
        return jsonify({"error": f"{llm.key_var()} not set — add it to .env and restart."}), 400

    web_search = body.get("web_search")     # None -> env default; bool -> override
    image_backend = body.get("image_backend")  # None / "template" / "openai"
    image_template = body.get("image_template")  # None / "auto" / a template id
    formats_map = body.get("formats") if isinstance(body.get("formats"), dict) else {}
    # If the topic was picked from the trend board, look up the state it is in
    # — server-side, never taken from the client. It steers the angle (a
    # peaked story needs a different take than a rising one) and is stamped
    # on the run for analytics.
    trend_id = body.get("trend_topic_id")
    trend = trends.service.topic(trend_id) if isinstance(trend_id, int) else None
    try:
        result = orchestrator.run(topic, platforms_selected=selected,
                                  use_search=web_search, image_backend=image_backend,
                                  image_template=image_template, formats_map=formats_map,
                                  trend_state=trend["state"] if trend else None)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Generation failed: {exc}"}), 500

    run_id = uuid.uuid4().hex
    frozen = manifest.build(result, approved_by="dashboard")
    meta = {"formats": {k: v for k, v in formats_map.items() if isinstance(v, str)},
            "trend_topic_id": trend["id"] if trend else None,
            "trend_state": trend["state"] if trend else None}
    _RUNS[run_id] = {"frozen": frozen, "review": result["review"], "meta": meta}
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
        "platforms": _postability(result["review"], result["drafts"],
                                  result["brief"].get("sources")),
        "trend_state": trend["state"] if trend else None,
        "writer": llm.last_writer, "writer_skipped": llm.last_errors,
        "mode": {"live": config.live, "auto": config.auto_publish},
    })


@app.post("/api/publish")
def api_publish():
    body = _body()
    run_id = body.get("run_id")
    platform = body.get("platform")
    confirm = bool(body.get("confirm"))

    run = _RUNS.get(run_id) if isinstance(run_id, str) else None
    err = _enforce(run, platform, confirm)  # never trust the client's button state
    if err:
        return err

    live = policy.Config.from_env().live
    result = BY_KEY[platform].publish(run["frozen"], live=live)
    _log_post(platform, run, result)
    return jsonify(result), 200


def _window_arg():
    """?window=<hours> if it is one of the configured options, else None (default)."""
    raw = request.args.get("window", "")
    try:
        hours = float(raw)
    except ValueError:
        return None
    return hours if hours in trends.window_options(trends.load_config()) else None


@app.get("/api/trends")
def api_trends():
    try:
        return jsonify(trends.service.board(hours=_window_arg()))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Trend board failed: {exc}"}), 500


@app.post("/api/trends/collect")
def api_trends_collect():
    started, reason = trends.service.trigger()
    return jsonify({"started": started, "reason": reason})


@app.get("/api/trends/updates")
def api_trends_updates():
    """The periodic digest: latest by default, or ?id=<n> for a past one."""
    raw = request.args.get("id")
    update_id = None
    if raw is not None:
        # isdigit() alone lets through a 20-digit number that overflows SQLite's
        # integer and crashes the query.
        if not raw.isdigit() or len(raw) > 12:
            return jsonify({"error": "Unknown update."}), 404
        update_id = int(raw)
    update = trends.get_update(update_id, _window_arg())
    if raw is not None and update is None:
        return jsonify({"error": "Unknown update."}), 404
    cfg = trends.load_config()
    return jsonify({"update": update, "updates": trends.list_updates(),
                    "next_due": trends.update_due(cfg=cfg)[1],
                    "report_hours": float(cfg.get("report_hours") or 24),
                    "window_options": trends.window_options(cfg)})


@app.get("/api/trends/history")
def api_trends_history():
    day = request.args.get("day", "")
    days = trends.history_days()
    if not day:
        return jsonify({"days": days, "topics": []})
    if day not in days:   # also rejects anything that is not a stored YYYY-MM-DD
        return jsonify({"error": "No history for that day."}), 404
    return jsonify({"days": days, "day": day, "topics": trends.history(day)})


@app.get("/api/content/formats")
def api_content_formats():
    from engine import content
    return jsonify({"platforms": content.platforms(), "languages": content.languages(),
                    "recent": content.recent()})


@app.post("/api/content")
def api_content():
    """Content generator. analyse_only=true returns the trend evidence without
    an LLM call (free); otherwise it also writes the package."""
    from engine import content
    body = _body()
    platform = body.get("platform")
    raw_topic = body.get("topic")
    topic = " ".join(raw_topic.split()) if isinstance(raw_topic, str) else ""
    notes = body.get("notes") if isinstance(body.get("notes"), str) else ""
    notes = notes[:4000]
    language = body.get("language") if body.get("language") in content.languages() else "english"
    if not isinstance(platform, str) or platform not in {p["id"] for p in content.platforms()}:
        return jsonify({"error": "Pick a platform."}), 400
    if sum(ch.isalnum() for ch in topic) < 3:
        return jsonify({"error": "Say what the video is about — a few words naming the subject."}), 400
    if len(topic) > 200:
        return jsonify({"error": "Keep the topic to one line (200 characters); "
                                 "put the detail in the notes box."}), 400
    try:
        board = trends.service.board()
    except Exception:  # noqa: BLE001 — the analysis still works without the live state
        board = None
    try:
        if body.get("analyse_only"):
            return jsonify(content.analyse(platform, topic, board))
        if not llm.has_api_key():
            return jsonify({"error": f"{llm.key_var()} not set — add it to .env and restart. "
                                     "“Analyse trends” works without it."}), 400
        out = content.generate(platform, topic, notes, language, board)
        return jsonify({**out, "writer": llm.last_writer, "writer_skipped": llm.last_errors})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Content generator failed: {exc}"}), 500


@app.get("/api/analytics")
def api_analytics():
    return jsonify({"posts": analytics.list_posts(), "summary": analytics.summary()})


@app.post("/api/analytics/metrics")
def api_analytics_metrics():
    body = _body()
    post_id = body.get("post_id")
    if not isinstance(post_id, str):
        return jsonify({"error": "Unknown post."}), 404
    try:
        ok = analytics.add_metrics(post_id, body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not ok:
        return jsonify({"error": "Unknown post."}), 404
    return jsonify({"saved": True})


@app.post("/api/trending")
def api_trending():
    if not llm.has_api_key():
        return jsonify({"error": f"{llm.key_var()} not set."}), 400
    category = str(_body().get("category") or "all")
    try:
        from engine import trending
        return jsonify({"topics": trending.suggest(category=category)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Trending fetch failed: {exc}"}), 500


@app.get("/api/trending/categories")
def api_trending_categories():
    from engine.trending import CATEGORIES
    return jsonify({"categories": [{"id": k, "name": v["name"]}
                                   for k, v in CATEGORIES.items()]})


@app.post("/api/schedule")
def api_schedule():
    body = _body()
    run_id = body.get("run_id")
    platform = body.get("platform")
    confirm = bool(body.get("confirm"))
    when_epoch = body.get("when_epoch")
    when_label = body.get("when_label", "")

    run = _RUNS.get(run_id) if isinstance(run_id, str) else None
    err = _enforce(run, platform, confirm)
    if err:
        return err
    try:
        when_epoch = float(when_epoch)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid schedule time."}), 400
    # NaN fails every comparison, so it would slip past the past-time check and
    # sit pending forever; inf never comes due. Both must be rejected here.
    if not math.isfinite(when_epoch):
        return jsonify({"error": "Invalid schedule time."}), 400
    if when_epoch <= time.time() + 5:
        return jsonify({"error": "Schedule time must be in the future."}), 400

    live = policy.Config.from_env().live
    job = scheduler.add(platform, when_epoch, str(when_label)[:40], run["frozen"], live,
                        meta=run.get("meta"))
    return jsonify({"status": "scheduled", "job": job}), 200


@app.get("/api/scheduled")
def api_scheduled():
    return jsonify({"jobs": scheduler.list()})


@app.post("/api/scheduled/cancel")
def api_scheduled_cancel():
    jid = _body().get("id")
    return jsonify({"cancelled": scheduler.cancel(jid)})


if __name__ == "__main__":
    import threading
    import webbrowser

    url = "http://127.0.0.1:5000"
    # Open the browser shortly after the server starts listening.
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Media-Engine dashboard -> {url}")
    app.run(host="127.0.0.1", port=5000, debug=False)
