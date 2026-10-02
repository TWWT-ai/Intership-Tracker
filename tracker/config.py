"""Config and path loading. Everything reads its settings through here."""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
STATE_DIR = DATA_DIR / "state"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
LOG_DIR = DATA_DIR / "logs"

WORKBOOK = DATA_DIR / "internships.xlsx"
PROFILE = DATA_DIR / "profile.json"
ID_REGISTRY = STATE_DIR / "id_registry.json"
# One review packet per tracker ID (tracker/prep.py). Gitignored: personal.
APPLICATIONS_DIR = DATA_DIR / "applications"
# The hackathon tracker (tracker/events.py): a separate workbook and state.
EVENTS_WORKBOOK = DATA_DIR / "hackathons.xlsx"
EVENTS_STORE = STATE_DIR / "events.json"
EVENTS_LEADS = STATE_DIR / "event_leads.json"
EVENTS_PLAN = STATE_DIR / "event_sweep_plan.json"


@functools.cache
def load(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml. Cached: config does not change mid-run."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_profile() -> dict[str, Any]:
    """The candidate profile. A missing or unreadable file is not fatal — the
    matcher just runs in relevance-only mode, which is also the state when the
    file exists but has not been filled in yet."""
    if not PROFILE.exists():
        return {"profile_configured": False}
    try:
        with PROFILE.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        return {"profile_configured": False, "_error": "profile.json is not valid JSON"}


def profile_is_configured(profile: dict[str, Any]) -> bool:
    """True only when the user has explicitly opted in.

    Deliberately strict: an accidentally half-filled profile must not switch on
    full scoring, because that would produce match percentages backed by
    nothing. The flag is the single source of truth.
    """
    return bool(profile.get("profile_configured"))


def env_override(key: str, default: Any = None) -> Any:
    """Read an INTERNSHIP_TRACKER_* environment override."""
    return os.environ.get(f"INTERNSHIP_TRACKER_{key}", default)


def ensure_dirs() -> None:
    for d in (DATA_DIR, STATE_DIR, SNAPSHOT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
