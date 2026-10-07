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

## Trend tracker (left panel)

While the dashboard runs, a background collector reads free public feeds every
30 minutes — no API key, no LLM — and the left panel shows what is **Emerging /
Rising / Peaking / Fading** in your niche. Click a topic to use it.

| Source | What it measures |
|---|---|
| Google Trends (India "trending now") | search volume per trending query |
| Google News (business section + your queries) | articles and distinct publishers per story |
| YouTube channel feeds (your watch-list) | views gained per video between collections |
| Wikipedia (top pages, India) | daily page views, as confirmation only |

How a state is decided — all in `engine/trends/lifecycle.py`:

- Items are grouped into topics by shared wording (`topics.py`).
- A topic's score is its **share of voice**: the fraction of each source's
  activity in a time window that is about it. Raw counts fall every night;
  a share only moves when the topic really gains or loses ground.
- The last window is compared with the one before it (9h, widening to 18h or
  24h when the flow is too thin), using only sources present in both.
- Rising = share up ≥30%. Fading = down ≥30%. Peaking = anything in between.
  Emerging = first seen within 6h and carried by ≤5 publishers/channels.
- **Every 9 hours an update is taken and stored** ("9-hour update" page): each
  topic classed Trending / Same / Fading, how it moved since the previous
  update (new, moved up, moved down, dropped off), and how well the window was
  actually observed. If nothing was collecting when one fell due, a single late
  update is taken on the next run and marked late — none are back-filled.
- `python -m engine.trends` runs one cycle without the dashboard; schedule it
  (Windows Task Scheduler) to keep updates coming while the dashboard is closed.
- A topic needs two publishers, two channels, or a Google trend to be shown.

Limits worth knowing: grouping is by words, not meaning, so a topic can pick up
an unrelated headline or a story can stay split in two; collection only happens
while the app is running; Instagram, Facebook, LinkedIn and Reddit expose no
usable free trend data and are not covered.

Everything is editable in `config/trends.yaml` (feeds, channels, thresholds).
Data lives in `data/` (git-ignored):

- `data/trends.db` — items, measurements, topics, every lifecycle change, and a
  permanent one-row-per-topic-per-day history ("Past days" in the panel).
- `data/analytics.db` — every post you publish, stamped with the trend state its
  topic was in, plus the result numbers you enter ("My posts & results"). Over
  time this shows whether posting on *rising* really beats *peaking* for you.

## Free models (writer fallback chain)

Every writing call can run on a free model first and fall back to a paid one
only when the free one is rate-limited or down. Set in `.env`:

```
GEMINI_API_KEY=...            # free, no card
MEDIA_ENGINE_WRITERS=gemini:gemini-3.5-flash-lite,gemini:gemini-flash-latest,openai:gpt-4o-mini
```

Any OpenAI-compatible provider works (`gemini`, `openrouter`, `groq`,
`deepseek`, `kimi`, `mistral`, plus `openai` and `anthropic`). The dashboard
shows which model wrote each result and which were skipped. Free tiers may use
your prompts to improve their products, and their limits and model lists change
without notice — see `.env.example` for what was measured and when. Live web
research still uses OpenAI: Gemini's search grounding refused a free-tier key.

## Content generator (packaging a video you already made)

Left menu → **Content generator**. Pick the platform (YouTube, Shorts, Instagram
Reel, Facebook, X, LinkedIn), say in one line what the video is about, list
what it covers, and get the title options, description/caption, tags, hashtags,
thumbnail text and pinned comment in that platform's format and limits
(`config/content_formats.yaml`).

- **Analyse trends (free, no LLM)** — what the trend store knows about the
  subject: live state, competing videos ranked by views per hour, recent
  headlines, word pairs in use, items per day, earlier days on the board, plus
  live YouTube/Google search suggestions (the wording viewers actually type).
- **Suggest** — one LLM call writes the package from that evidence. Code then
  enforces the hard limits, and lints the copy for filler, thumbnail text that
  repeats the title, and figures that are not in your notes; a failed lint
  triggers one automatic rewrite, and anything still wrong is shown.

The package only describes what you list in the notes box — leave it empty and
the copy stays general.

## Writing for the ranking algorithms

`config/algorithms.yaml` holds what each platform's ranking is known to reward,
every rule tagged `official` / `code` / `reported` / `heuristic` so it is clear
how solid it is. Algorithms change — re-check it every few months.

- **Drafting** — the platform's rules are added to the writing prompt (no link
  in the body on X and LinkedIn, hook in the first line, end on a real question,
  hashtag limits, no engagement bait).
- **Algorithm fit** — every draft is checked by code against those rules and
  gets a 0-100 score with the reasons, shown on its card. Advisory only: it
  never blocks a post; the review gate decides that.
- **Trend stage** — a topic picked from the trend board carries its stage into
  angle selection: rising gets the timely take, peaking a contrarian one,
  fading a "what happens next".

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Offline suite (LLM mocked, no API cost) covering the policy scenario matrix,
the review gate, the scheduler, publishers, the manifest, dashboard
enforcement, and the trend tracker (parsers, topic grouping, lifecycle maths,
collection with injected feeds, the analytics store).

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
