# Media-Engine

Give it one topic. It researches the freshest trending coverage, picks the best
honest angle, and writes a platform-native post for **Instagram**, **Twitter/X**,
and **LinkedIn** — automatically.

```
python cli.py "Iran-Iraq war"
```

## Where this is (v4)

Full pipeline + an auto-publish policy + a web dashboard.

```bash
python app.py                              # dashboard  -> http://127.0.0.1:5000
python cli.py "Iran-Iraq war"              # manual: run + human y/N approve
python cli.py --auto "Iran-Iraq war"       # apply the auto-publish policy
python cli.py --queue topics.example.txt   # batch/scheduler over many topics
```

**Dashboard** (`app.py` + `templates/index.html`, auto-opens the browser):
type a topic, pick which platforms to generate (Twitter / Instagram /
LinkedIn), and choose **Post now** or **Schedule** for a future time. Drafts +
Instagram image + review verdict appear; post or schedule per platform.

- Each **Post / Schedule** button reflects the safety policy — green for PASS,
  amber "Review & …" (with a confirm) for sensitive/HOLD, red disabled for
  REVISE. The server re-enforces every rule; it never trusts the button.
- **Scheduled posts** run on a background thread and are persisted to
  `output/scheduled/` (survive a restart while the app is running). A live
  panel lists pending jobs with a Cancel button. Past times are rejected.
- Scheduler ticks only while the app is open (real cron = a later milestone).

Publishing is still **dry-run by default** — nothing is sent until you set
`MEDIA_ENGINE_LIVE=true`, add a platform's credentials, and wire its
`_send()`. Sensitive topics and non-PASS drafts are **never** auto-fired.

### Auto-publish decision (per platform)

| Condition | Action |
|-----------|--------|
| platform disabled | SKIP |
| **sensitive topic, or verdict HOLD** | **HOLD for human** (hard rule) |
| verdict REVISE (still failing) | BLOCK |
| auto-publish off | HOLD for human |
| dry-run mode | DRY_RUN |
| live but no credentials | SKIP |
| clean + authorized + live | PUBLISH |

Pipeline (`engine/`):

| Stage | File | What it does |
|-------|------|--------------|
| 1. Research | `research.py` | Web-searches live news, distills a structured brief (facts, trending keywords, hashtags, sentiment, sensitivity flags, sources) |
| 2. Angle | `angle.py` | Picks one honest hook so all three posts tell one story |
| 3. Draft | `drafters.py` | Writes each post to its own style + limits from `config/platforms.yaml` |
| 4. Image | `imagegen.py` | Renders a 1080×1080 Instagram card (Pillow, local) — legible headline over a topic-fit gradient |
| 5. Gate | `review.py` | Validates limits (code) + self-critiques facts/tone/sensitivity (Claude) + auto-revises hard-limit breaks once; emits a PASS/REVISE/HOLD verdict |
| Approve | `cli.py` + `manifest.py` | Human `y/N` sign-off; forced for sensitive topics. Approval freezes an `output/<slug>.approved.json` manifest |
| 6. Publish | `publish.py` + `publishers/` | Reads the manifest, builds the exact API call per platform (X / IG / LinkedIn). **Dry-run** — prints what it *would* post, sends nothing |
| 7. Policy | `policy.py` + `autopost.py` | Auto-publish decision engine. Per platform: PUBLISH / DRY_RUN / HOLD / BLOCK / SKIP. **Sensitive topics + non-PASS verdicts can never auto-fire** |

Powered by Claude Opus 4.8 with the built-in web-search tool (the "auto-detect
trending" engine). Edit tone and character limits in `config/platforms.yaml` —
no code change needed.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # then add ONE provider key
python app.py                     # dashboard, or: python cli.py "your topic"
```

**LLM provider** — set one key in `.env`; the provider auto-detects (OpenAI
preferred), or force it with `MEDIA_ENGINE_PROVIDER=openai|anthropic`:

- **OpenAI** (default): `OPENAI_API_KEY`, model `gpt-4o` — web search via the
  Responses API `web_search_preview` tool, structured JSON via
  `response_format: json_schema`.
- **Anthropic**: `ANTHROPIC_API_KEY`, model `claude-opus-4-8`.

Only `engine/llm.py` is provider-specific; the rest of the pipeline is
provider-agnostic.

**Cost** — default `gpt-4o-mini`. Two big levers:

- **Web search** is ~80% of per-run cost. `MEDIA_ENGINE_WEB_SEARCH=off` →
  near-free (~$0.005/run) using the model's own knowledge (not live-trending);
  `on` → live trending news (~$0.02/run).
- **Model** — bump `MEDIA_ENGINE_OPENAI_MODEL` to `gpt-5.5` (~$1/run) only when
  top writing quality matters.

All platform drafts are produced in a single combined call (fewer tokens than
one call each). Set spend alerts + a monthly cap in the OpenAI dashboard.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Offline suite (LLM mocked, no API cost) covering the policy scenario matrix,
the review gate, the scheduler, publishers, the manifest, and dashboard
enforcement.

## Roadmap

- ~~**v1** — render the Instagram image~~ ✅ done
- ~~**v2** — safety/quality gate + human approve (`y/n`)~~ ✅ done
- ~~**v3** — publishers (X / IG / LinkedIn), dry-run~~ ✅ done
- ~~**v4** — auto-publish policy engine + `--auto` + `--queue` batch~~ ✅ done
- ~~**Dashboard** — web UI, per-platform Post buttons, policy-aware~~ ✅ done
- **v3-live** — wire `_send()` + credentials per platform, one at a time (first real post)
- **v4+** — real cron/scheduler (OS task / `schedule` lib) driving `--queue`

> ⚠️ Auto-posting on sensitive topics (war, politics) carries real legal/brand
> risk. The human gate (v2) stays on for flagged topics until you trust the
> output.
