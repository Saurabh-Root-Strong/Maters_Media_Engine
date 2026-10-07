import os
import sys

import pytest

# Make `engine` and `app` importable when pytest runs from the project root.
sys.path.insert(0, os.path.dirname(__file__))

# Must be set before `app` is imported: the suite must never start the
# background thread that loads + fires the user's real scheduled jobs.
os.environ["MEDIA_ENGINE_SCHEDULER"] = "off"
# ...nor the trend collector, which would hit the network.
os.environ["MEDIA_ENGINE_TRENDS"] = "off"
# The developer's .env must not leak in: set before `app` loads it (load_dotenv
# never overrides a variable that already exists), so no test can reach a real
# provider through a configured fallback chain.
os.environ["MEDIA_ENGINE_WRITERS"] = ""
os.environ["MEDIA_ENGINE_SEARCH"] = ""
os.environ["YOUTUBE_API_KEY"] = ""      # tests choose the YouTube path themselves


@pytest.fixture(autouse=True)
def _isolate_output(tmp_path, monkeypatch):
    """Point every on-disk store at a temp dir so tests never touch real output/ or data/."""
    from engine import analytics, memory
    from engine import scheduler as sched_mod
    from engine.trends import store as trend_store

    monkeypatch.setattr(memory, "_HISTORY_PATH", str(tmp_path / "history.json"))
    monkeypatch.setattr(sched_mod, "_DIR", str(tmp_path / "scheduled"))
    monkeypatch.setattr(trend_store, "DB_PATH", str(tmp_path / "trends.db"))
    monkeypatch.setattr(analytics, "DB_PATH", str(tmp_path / "analytics.db"))
