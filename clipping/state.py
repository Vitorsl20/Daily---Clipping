"""Cross-run memory for stale-feed detection, so a feed stuck for several days shows up as a pattern."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "feed_health.json"


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def record(state: dict, feed: str, stale: bool, newest: datetime | None, run_day: str) -> int:
    """Update a feed's streak; returns how many consecutive runs it has been stale (0 if fresh)."""
    entry = state.get(feed, {"consecutive_stale": 0, "stale_since": None, "last_run": None})
    if entry.get("last_run") == run_day:  # re-run on the same day: don't double count
        entry["consecutive_stale"] = max(entry["consecutive_stale"] - (1 if entry.get("last_stale") else 0), 0)
        if entry["consecutive_stale"] == 0:
            entry["stale_since"] = None
    if stale:
        entry["consecutive_stale"] += 1
        entry["stale_since"] = entry["stale_since"] or run_day
    else:
        entry["consecutive_stale"] = 0
        entry["stale_since"] = None
    entry["last_run"] = run_day
    entry["last_stale"] = stale
    entry["newest_item"] = newest.isoformat() if newest else None
    state[feed] = entry
    return entry["consecutive_stale"]
