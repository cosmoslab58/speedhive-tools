"""Per-organization settings resolution, shared by the CLI and any host
application (e.g. speedhive-tools-ui).

Every organization's config -- Anthropic keys, the announcer-text parsing
engine, the minimum-lap threshold for consistency stats, and (for a host
application) its own overrides -- lives in a single JSON file:

    <SPEEDHIVE_DATA_DIR>/orgs/<org_id>/settings.json

`SPEEDHIVE_DATA_DIR` defaults to `./data` (relative to the current working
directory), so this resolves identically for a bare `pip install
speedhive-tools` CLI invocation and for a host application that points the
env var at its own data directory -- there is no dependency on any
particular app.

This module only knows about settings speedhive-tools itself has code paths
for (Anthropic keys, parsing engine, min-laps). It never references
notification/email settings (RESEND_API_KEY, NOTIFICATION_*) -- those are
policy owned entirely by whatever host application is built on top, resolved
through the same generic get_org_env_var/set_org_env_var functions below.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


def data_root() -> Path:
    return Path(os.environ.get("SPEEDHIVE_DATA_DIR", "./data"))


def org_settings_path(org_id: int) -> Path:
    return data_root() / "orgs" / str(org_id) / "settings.json"


def read_org_settings(org_id: int) -> Dict[str, Any]:
    path = org_settings_path(org_id)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def write_org_settings(org_id: int, config: Dict[str, Any]) -> None:
    path = org_settings_path(org_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_org_env_var(name: str, org_id: int) -> Optional[str]:
    """The effective value for actually USING a setting: this org's own
    override if set, otherwise the shared bare-name fallback."""
    return get_org_env_var_override(name, org_id) or os.environ.get(name)


def get_org_env_var_override(name: str, org_id: int) -> Optional[str]:
    """This org's own explicit value only -- never the shared fallback. Use
    this (not get_org_env_var) to populate a settings form field: showing
    the shared/global secret's value in a per-org field would look like it
    belongs to this org, and silently pins it as an org-specific override
    the next time the form is saved."""
    override = read_org_settings(org_id).get("overrides", {}).get(name)
    if override:
        return override
    return os.environ.get(f"{name}_{org_id}")


def has_global_default(name: str) -> bool:
    return bool(os.environ.get(name))


def get_org_env_var_with_source(name: str, org_id: int) -> Tuple[Optional[str], Optional[str]]:
    """Returns (effective_value, source), where source is 'org' (this org's
    own override), 'global' (the shared fallback), or None (not configured
    anywhere) -- for building a "how is this actually configured" summary,
    as opposed to get_org_env_var_override()'s "what should the edit form
    show" (which never reveals the shared value)."""
    org_value = get_org_env_var_override(name, org_id)
    if org_value:
        return org_value, "org"
    global_value = os.environ.get(name)
    if global_value:
        return global_value, "global"
    return None, None


def set_org_env_var(name: str, org_id: int, value: Optional[str]) -> None:
    """Persist `value` under the org-scoped overrides block in its settings.json,
    and apply it to the current process environment immediately so an
    already-running process reflects the change without needing a restart."""
    config = read_org_settings(org_id)

    if "overrides" not in config:
        config["overrides"] = {}

    if value:
        config["overrides"][name] = value
    else:
        config["overrides"].pop(name, None)

    if not config["overrides"]:
        config.pop("overrides", None)

    write_org_settings(org_id, config)

    key = f"{name}_{org_id}"
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)


def get_parsing_engine(org_id: int) -> str:
    """The announcer-text parser configured for this org's track-record
    scans: 'regex' (default) or 'llm' (Claude), set via 'parsing.engine' in
    the org's settings.json."""
    engine = (read_org_settings(org_id).get("parsing") or {}).get("engine")
    return engine if engine == "llm" else "regex"


def get_bulk_parser_for_org(org_id: int) -> Optional[Callable]:
    """Return the bulk announcement parser configured for this org's scans.

    Regex is the default for every org -- LLM (Claude) is opt-in per org via
    'parsing.engine': 'llm' in the org's settings.json. When LLM is active,
    all of the org's announcements are parsed in a single call rather than
    one call per announcement -- storage.get_track_records() falls back to
    the regex parser (one call per text, but nearly instant) when this
    returns None.
    """
    if get_parsing_engine(org_id) != "llm":
        return None
    import functools
    from speedhive.llm import parse_track_records_bulk_with_claude
    return functools.partial(parse_track_records_bulk_with_claude, org_id=org_id)


def get_stats_min_laps(org_id: int) -> int:
    """Minimum lap count for a driver to appear in consistency-analyzer
    rankings, set via 'stats.min_laps' in the org's settings.json."""
    try:
        return int((read_org_settings(org_id).get("stats") or {}).get("min_laps", 20))
    except Exception:
        return 20
