"""Shared filesystem helpers for per-org track-record workflow state."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from speedhive.ndjson import atomic_write_text, load_ndjson, save_ndjson


def org_track_records_dir(track_records_root: Path, org_id: int) -> Path:
    return Path(track_records_root) / str(org_id) / "track_records"


def paths_for_org(track_records_root: Path, org_id: int) -> Dict[str, Path]:
    d = org_track_records_dir(track_records_root, org_id)
    return {
        "dir": d,
        "curated": d / "curated.ndjson",
        "candidates": d / "candidates_pending.ndjson",
        "rejected": d / "rejected.ndjson",
        "alias_map": d / "class_alias_map.json",
        "parse_cache": d / "announcement_parse_cache.json",
        "history": d / "history",
        "tasks": d / "tasks",
    }


def load_json(path, default):
    if not Path(path).exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_curated(p):
    return load_ndjson(p["curated"], {"date": None, "records": []}, "records")


def save_curated(p, doc):
    save_ndjson(p["curated"], doc, "records")


def load_candidates(p):
    return load_ndjson(p["candidates"], {"generated_at": None, "org_id": None, "candidates": []}, "candidates")


def save_candidates(p, doc):
    save_ndjson(p["candidates"], doc, "candidates")


def load_rejected(p):
    return load_ndjson(p["rejected"], {"rejected": []}, "rejected")


def save_rejected(p, doc):
    save_ndjson(p["rejected"], doc, "rejected")


def load_parse_cache(p):
    return load_json(p["parse_cache"], {"engine": None, "cache": {}})


def save_parse_cache(p, doc):
    save_json(p["parse_cache"], doc)
