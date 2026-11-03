"""The memory engine — grading calls, video facts, title patterns, topic history, past work."""

import json
from contextlib import closing
from datetime import datetime, timezone

import pytest

import app as app_module
from engine import content, recall
from engine.trends import lookup, service, store
from tests.test_trends import CFG, H, HYFUN, JIO, NOW, TRENT, _filler, _story, fetcher

DAY = 86400


# --- grading the trend calls ----------------------------------------------------

def _calls_then_outcome():
    """At NOW: Jio flat (PEAKING), Trent +300% (RISING), HyFun -50% (FADING).
    Six hours on: Jio holds its share, Trent and HyFun go silent."""
    first = (_story(JIO, 10, 9, "a") + _story(TRENT, 2, 9, "b") + _story(HYFUN, 4, 9, "c") + _filler(8, 9, "p")
             + _story(JIO, 5, 3, "d") + _story(TRENT, 4, 3, "e") + _story(HYFUN, 1, 3, "f") + _filler(2, 3, "r"))
    service.collect(now=NOW, cfg=CFG, fetch=fetcher(news=first))
    service.compute(now=NOW, cfg=CFG, write=True)
    later = NOW + 6 * H
    service.collect(now=later, cfg=CFG, fetch=fetcher(news=_story(JIO, 6, 3, "g") + _filler(8, 3, "s"), base=later))
    return later


def test_calls_record_the_window_they_were_made_on():
    _calls_then_outcome()
    with closing(store.connect()) as conn:
        rows = conn.execute("SELECT state, window_hours FROM topic_states").fetchall()
    assert {r["state"] for r in rows} == {"PEAKING", "RISING", "FADING"}
    assert all(r["window_hours"] == 6 for r in rows)


def test_a_call_is_not_graded_before_its_window_has_passed():
    later = _calls_then_outcome()
    assert recall.grade_calls(now=later - H, cfg=CFG) == 0
    assert recall.scorecard()["graded"] == 0 and recall.scorecard()["pending"] == 3
    assert recall.grade_calls(now=later, cfg=CFG) == 3
    assert recall.grade_calls(now=later, cfg=CFG) == 0             # never graded twice


def test_each_call_is_graded_against_what_the_topic_did_next():
    later = _calls_then_outcome()
    recall.grade_calls(now=later, cfg=CFG)
    with closing(store.connect()) as conn:
        got = {r["state"]: (r["outcome"], r["change_pct"]) for r in conn.execute("SELECT * FROM call_grades")}
    assert got["PEAKING"][0] == "held"                             # Jio kept its share
    assert got["RISING"] == ("dropped", -100)                      # the rising call was wrong: Trent went silent
    assert got["FADING"][0] in ("dropped", "gone")                 # the fading call was right


def test_scorecard_sets_every_hit_rate_against_the_base_rate():
    later = _calls_then_outcome()
    recall.grade_calls(now=later, cfg=CFG)
    card = {s["state"]: s for s in recall.scorecard()["states"]}
    assert card["EMERGING"] == {"state": "EMERGING", "n": 0}
    assert (card["RISING"]["n"], card["RISING"]["right_pct"]) == (1, 0)
    assert card["PEAKING"]["right_pct"] == 100 and card["FADING"]["right_pct"] == 100
    # 1 of the 3 graded calls "held or grew" -> a rising call must beat 33%, and here it did not
    assert card["RISING"]["base_pct"] == 33 and card["RISING"]["lift_pts"] == -33
    assert card["RISING"]["reliable"] is False                     # one call is an anecdote


def test_calls_made_before_the_window_was_recorded_are_left_ungraded():
    with closing(store.connect()) as conn:
        conn.execute("INSERT INTO topic_states (topic_id, ts, state, score, velocity) VALUES (1, ?, 'RISING', 0.1, 0.5)",
                     (NOW - 10 * DAY,))
        conn.commit()
    assert recall.grade_calls(now=NOW, cfg=CFG) == 0
    assert recall.scorecard()["ungradable_old"] == 1


def test_a_merged_topic_is_graded_under_the_id_it_became():
    later = _calls_then_outcome()
    with closing(store.connect()) as conn:
        old = conn.execute("SELECT topic_id FROM topic_states WHERE state='PEAKING'").fetchone()[0]
        new = conn.execute("INSERT INTO topics (tokens, n_items, first_seen, last_seen, niche)"
                           " SELECT tokens, n_items, first_seen, last_seen, niche FROM topics WHERE id=?",
                           (old,)).lastrowid
        conn.execute("UPDATE items SET topic_id=? WHERE topic_id=?", (new, old))
        conn.execute("DELETE FROM topics WHERE id=?", (old,))
        conn.execute("INSERT INTO topic_merges (old_id, new_id) VALUES (?,?)", (old, new))
        conn.commit()
    recall.grade_calls(now=later, cfg=CFG)
    with closing(store.connect()) as conn:
        assert conn.execute("SELECT outcome FROM call_grades WHERE state='PEAKING'").fetchone()[0] == "held"


# --- video facts + title patterns -----------------------------------------------

def _facts(rows):
    with closing(recall._connect()) as conn:
        conn.executemany("INSERT INTO video_facts (item_id, channel, grp, kind, title, published_at, views,"
                         " age_hours, outlier, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [(i, "Ch", grp, "long", title, NOW, 1000, age, out, NOW)
                          for i, (title, out, grp, age) in enumerate(rows)])
        conn.commit()


def test_title_patterns_compare_like_with_like_and_report_sample_sizes():
    _facts([(f"Why is stock{i} falling?", 2.0, "creator", 48) for i in range(30)]
           + [(f"Stock{i} update", 1.0, "creator", 48) for i in range(30)]
           + [("Why did this news clip do well?", 9.0, "news", 48)] * 5          # other group: ignored
           + [("Why is this brand new?", 9.0, "creator", 2)] * 5)                # too young to judge: ignored
    p = recall.title_patterns("creator")
    q = next(x for x in p["patterns"] if x["trait"] == "asks a question")
    assert p["videos"] == 60
    assert (q["n_with"], q["n_without"], q["with"], q["without"], q["effect"]) == (30, 30, 2.0, 1.0, 2.0)
    assert q["reliable"] is True and q["p"] < 0.01
    assert any("ask a question outrun the rest" in s and "(30 vs 30 videos)" in s for s in recall.lessons())


def test_a_difference_that_could_be_chance_is_not_called_reliable():
    """30 vs 30 videos whose paces overlap almost completely: medians differ, but the ranks do not."""
    import random
    rng = random.Random(7)
    with_q = [(f"Why is stock{i} falling?", round(rng.uniform(0.5, 2.0), 2), "creator", 48) for i in range(30)]
    without = [(f"Stock{i} update", round(rng.uniform(0.5, 2.0), 2), "creator", 48) for i in range(30)]
    _facts(with_q + without)
    q = next(x for x in recall.title_patterns()["patterns"] if x["trait"] == "asks a question")
    assert q["n_with"] == q["n_without"] == 30 and q["p"] > 0.05 and q["reliable"] is False
    assert recall.lessons() == []


def test_rank_test_matches_known_values():
    # identical samples -> no evidence; fully separated samples of 30 -> overwhelming
    assert recall._rank_test([1, 2, 3, 4] * 8, [1, 2, 3, 4] * 8) == pytest.approx(1.0, abs=0.02)
    assert recall._rank_test([2.0] * 30, [1.0] * 30) < 1e-6
    # hand-worked: ranks of the first sample sum to 19, so U = 4 for n1 = n2 = 5;
    # z = (|4 - 12.5| - 0.5) / sqrt(22.92) = 1.67 -> two-sided p = 0.095
    assert recall._rank_test([1, 2, 3, 4, 9], [5, 6, 7, 8, 10]) == pytest.approx(0.0947, abs=0.01)


def test_thin_or_weak_patterns_never_become_lessons():
    _facts([(f"Why is stock{i} falling?", 3.0, "creator", 48) for i in range(4)]       # only 4: not reliable
           + [(f"Stock{i} update", 1.0, "creator", 48) for i in range(30)])
    q = next(x for x in recall.title_patterns()["patterns"] if x["trait"] == "asks a question")
    assert q["effect"] == 3.0 and q["reliable"] is False
    assert recall.lessons() == []


def test_snapshot_keeps_each_videos_pace_after_raw_readings_are_pruned():
    cfg = {**CFG, "youtube": [{"id": "a", "name": "Groww", "group": "creator"}]}
    with closing(store.connect()) as conn:
        for i, views in enumerate((1000, 1000, 1000, 5000)):
            cur = conn.execute("INSERT INTO items (source, ext_id, title, publisher, published_at, first_seen, kind)"
                               " VALUES ('youtube',?,?,?,?,?,'long')",
                               (f"v{i}", f"Video {i}", "Groww", NOW - 10 * H, NOW))
            conn.execute("INSERT INTO observations VALUES (?,?,?)", (cur.lastrowid, NOW, views))
        conn.commit()
    assert recall.snapshot_videos(now=NOW, cfg=cfg) == 4
    with closing(store.connect()) as conn:
        store.prune(conn, NOW + 40 * DAY)                           # raw readings gone
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        hot = conn.execute("SELECT grp, kind, views, outlier FROM video_facts WHERE title='Video 3'").fetchone()
    assert tuple(hot) == ("creator", "long", 5000, 5.0)


# --- per-topic history + seasonality --------------------------------------------

def topic_fetch(readers, monthly=None):
    def fetch(url):
        if "news.google.com" in url:
            return "<rss><channel></channel></rss>"
        if "w/api.php" in url:
            return json.dumps({"query": {"search": [{"title": "Harshad Mehta"}]}})
        if "/monthly/" in url:
            if monthly is None:
                raise OSError("no monthly data")
            return json.dumps({"items": [{"timestamp": f"{y}{m:02d}0100", "views": v} for (y, m), v in monthly.items()]})
        if "pageviews/per-article" in url:
            return json.dumps({"items": [{"views": readers}] * 60})
        raise OSError("unstubbed")
    return fetch


def test_every_look_at_a_topic_is_kept_and_the_change_is_reported():
    first = lookup.research("harshad mehta", now=NOW, fetch=topic_fetch(1000), youtube=False)
    assert first["history"] == []
    lookup.research("harshad mehta", now=NOW + H, fetch=topic_fetch(1000), youtube=False)        # cached: same look
    third = lookup.research("harshad mehta", now=NOW + 3 * DAY, fetch=topic_fetch(1500), youtube=False)
    assert len(third["history"]) == 1                              # the cached repeat did not add a row
    assert any("Since you last looked 3 days ago" in r and "1,000 → 1,500 a day (+50%)" in r
               for r in third["verdict"]["reasons"])
    seen = lookup.topics_seen()
    assert seen[0]["topic"] == "harshad mehta" and seen[0]["looks"] == 2
    assert (seen[0]["readers_first"], seen[0]["readers_last"]) == (1000, 1500)


def test_a_yearly_season_is_found_only_when_it_recurs():
    today = datetime.fromtimestamp(NOW, timezone.utc).date()
    months = [(y, m) for y in (today.year - 3, today.year - 2, today.year - 1) for m in range(1, 13)]
    october = {ym: (5000 if ym[1] == 10 else 1000) for ym in months}
    r = lookup.research("harshad mehta", now=NOW, fetch=topic_fetch(1000, october), youtube=False)
    season = r["interest"]["season"]
    assert season["peak_month_name"] == "October" and season["index"] == 5.0
    assert any("usually peaks in October" in x for x in r["verdict"]["reasons"])

    one_off = {ym: 1000 for ym in months}
    one_off[(today.year - 1, 10)] = 50_000                         # a single freak spike is not a season
    r = lookup.research("other subject mehta", now=NOW, fetch=topic_fetch(1000, one_off), youtube=False)
    assert r["interest"]["season"] is None


# --- remembering what was already made ------------------------------------------

def test_the_writer_is_told_what_was_already_written_and_repeats_are_caught(monkeypatch):
    prompts, replies = [], []

    def fake(system, user, schema, max_tokens=0):
        if "audience_intent" in schema["properties"]:
            prompts.append(user)
            return {"audience_intent": "a", "angle": "b", "promise": "c", "primary_keyword": "d",
                    "secondary_keywords": [], "specific_hashtags": [], "avoid": "e"}
        return replies.pop(0)
    monkeypatch.setattr(content.llm, "structured", fake)
    line = "How did Jio Platforms reach its IPO valuation?"
    replies.append({"description": "Jio Platforms IPO, explained.", "hashtags": ["JioIPO"],
                    "hooks": [{"text": line, "why": "w"}]})
    first = content.generate("twitter", "Jio IPO")
    assert first["past"] == [] and "ALREADY WRITTEN" not in prompts[0]

    replies += [{"description": "Jio Platforms IPO again.", "hashtags": ["JioIPO"], "hooks": [{"text": line, "why": "w"}]},
                {"description": "Jio Platforms IPO again.", "hashtags": ["JioIPO"],
                 "hooks": [{"text": "What does the Jio IPO mean for Reliance holders?", "why": "w"}]}]
    second = content.generate("twitter", "Jio IPO valuation")       # same subject, worded differently
    assert "ALREADY WRITTEN FOR THIS SUBJECT" in prompts[1] and line in prompts[1]
    assert second["past"][0]["lines"] == [line]
    assert second["revised"] is True and second["quality_notes"] == []     # the repeat was caught and rewritten
    assert second["package"]["hooks"][0]["text"].startswith("What does the Jio IPO mean")
    assert content.past_work("Trent Zudio expansion") == []                 # a different subject finds nothing
    made = content.made_summary()
    assert {m["topic"] for m in made} == {"Jio IPO", "Jio IPO valuation"}


def test_reliable_lessons_reach_the_planner(monkeypatch):
    _facts([(f"Why is stock{i} falling?", 2.0, "creator", 48) for i in range(30)]
           + [(f"Stock{i} update", 1.0, "creator", 48) for i in range(30)])
    seen = []
    monkeypatch.setattr(content.llm, "structured", lambda s, u, sc, max_tokens=0: seen.append(u) or (
        {"audience_intent": "a", "angle": "b", "promise": "c", "primary_keyword": "d", "secondary_keywords": [],
         "specific_hashtags": [], "avoid": "e"} if "audience_intent" in sc["properties"]
        else {"description": "Jio Platforms.", "hashtags": ["JioIPO"], "hooks": []}))
    out = content.generate("twitter", "Jio IPO")
    assert "LEARNED FROM THE WATCHED CREATORS" in seen[0] and "ask a question" in seen[0]
    assert out["learned"] and "association, not a rule" in seen[0]


# --- route -----------------------------------------------------------------------

def test_memory_route():
    d = app_module.app.test_client().get("/api/memory").get_json()
    assert d["scorecard"]["graded"] == 0 and d["patterns"]["patterns"] == []
    assert d["topics_seen"] == [] and d["made"] == [] and d["lessons"] == []
    assert set(d["size"]) >= {"items", "topics", "topic_states", "video_facts"}


@pytest.mark.parametrize("state", recall.GRADED_STATES)
def test_every_graded_state_has_a_promise(state):
    assert recall.PROMISE[state] <= {"grew", "held", "dropped", "gone"}
