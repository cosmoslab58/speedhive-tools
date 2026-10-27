"""Generic, per-organization track-records curation: sync/diff + storage.

Pulls Speedhive's announcer-flagged "New Track/Class Record" data for a given
org, normalizes classification tokens against that org's own alias map (no
hardcoded org-specific data lives here), and diffs against a per-org curated
file -- new/changed rows only ever land in a per-org candidates_pending.ndjson
for a human to review, never written to curated.ndjson directly.

Row-shaped data (curated records, pending candidates, rejected entries) is
stored as NDJSON (see speedhive.ndjson for the format convention);
config-shaped files (class alias map, notification settings, task state) are
plain JSON documents.

Works for any org_id; nothing here is specific to any one club/organization.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

from speedhive.ndjson import save_ndjson
from speedhive.utils.lap_analysis import normalize_classification, normalize_name
from speedhive.workflows.refresh_org_cache import refresh_org_cache
from speedhive.stores.track_records import (
    load_candidates,
    load_curated,
    load_json,
    load_parse_cache,
    load_rejected,
    paths_for_org,
    save_candidates,
    save_curated,
    save_json,
    save_parse_cache,
    save_rejected,
)

GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_APP_TOKEN = os.environ.get("GOTIFY_APP_TOKEN")
# Speedhive syncs are slow (lots of data per event) -- only re-sync if the
# cache is older than this, unless the caller explicitly forces it.
DEFAULT_STALE_AFTER_HOURS = float(os.environ.get("TRACK_RECORDS_STALE_HOURS", "20"))

def lap_time_to_seconds(lap_time):
    """Parse 'm:ss.mmm' or 'ss.mmm' into float seconds; None if unparseable.

    Stricter than analysis.lap_analysis.parse_time_value on purpose: this one
    is used to validate human-supplied lap times, so it must reject junk
    rather than salvage digits out of it.
    """
    if not lap_time:
        return None
    parts = str(lap_time).split(":")
    if len(parts) > 2:
        # e.g. "1:17:917" (colon typo'd in place of the ms period) -- a
        # truncated float(parts[0]) would silently produce an absurd record
        return None
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


# Only laps set in a race count as track records. Speedhive's session `type`
# can't be trusted on its own -- older uploads tag "Saturday Class Race" as
# qualify and drivers-school "Practice Starts and Races" as race -- so the
# session name decides first and the type only breaks a tie. Grid sheets are
# not races: their times are the qualifying laps the grid was built from.
_DUAL_PURPOSE_RACE = re.compile(
    r"race\b[^/]*/\s*scca qual|qualifying whrri race|whrri (class )?race \+ qualifying|race\s*\+\s*glc qual",
    re.IGNORECASE,
)
_NON_RACE_SESSION = re.compile(
    r"qualif|\bqual\b|grid|practice|preactice|warm ?up|lunch|break|time trial|\btt\b|bracket|\bbr\b"
    r"|test|transponder|setup|instructor|summary|\blist\b|overall results",
    re.IGNORECASE,
)
_RACE_SESSION = re.compile(
    r"race|feature|showcase|show case|invitational|enduro|endurance|shoot ?out|smoke|big.bore"
    r"|i\.?t\.? ?fest|challenge|co-?driver",
    re.IGNORECASE,
)


def is_race_session(session_name, session_type=None):
    """True if a session is a race, so a lap set in it can be a track record.

    With no name and no type there is no evidence either way, and the session
    is treated as a race rather than silently dropping its announcements.
    """
    name = (session_name or "").replace("_", " ")
    if _DUAL_PURPOSE_RACE.search(name):
        return True
    if _NON_RACE_SESSION.search(name):
        return False
    if _RACE_SESSION.search(name):
        return True
    return session_type in (None, "race")


def notify_gotify(title, message):
    if not GOTIFY_URL or not GOTIFY_APP_TOKEN:
        return
    try:
        data = urllib.parse.urlencode({"title": title, "message": message, "priority": 5}).encode()
        url = f"{GOTIFY_URL.rstrip('/')}/message?token={GOTIFY_APP_TOKEN}"
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as exc:
        print(f"Gotify notification failed: {exc}", file=sys.stderr)


def build_curated_fastest_index(curated):
    """Map classAbbreviation -> fastest curated record (dict, plus '_seconds')."""
    fastest = {}
    for r in curated.get("records", []):
        cls = r["classAbbreviation"]
        secs = lap_time_to_seconds(r.get("lapTime"))
        if secs is None:
            continue
        if cls not in fastest or secs < fastest[cls]["_seconds"]:
            entry = dict(r)
            entry["_seconds"] = secs
            fastest[cls] = entry
    return fastest


def normalize_identity_part(val):
    if val is None:
        return ""
    return str(val).strip().upper()


def make_ldc(cls, lap, driver):
    return (
        normalize_identity_part(cls),
        normalize_identity_part(lap),
        normalize_identity_part(driver)
    )


def rejected_key(classAbbreviation, lapTime, driverName, date):
    return (
        normalize_identity_part(classAbbreviation),
        normalize_identity_part(lapTime),
        normalize_identity_part(driverName),
        normalize_identity_part(date)
    )


LAP_TIME_MATCH_TOLERANCE_SECONDS = 0.01


def lap_times_match(a, b, tolerance=LAP_TIME_MATCH_TOLERANCE_SECONDS):
    """True if two lap-time strings represent the same time within a small
    tolerance, tolerating format differences the exact-string ldc check
    misses (e.g. curated '0:59.439' vs raw announcer '59.439', or
    hand-transcription rounding like '1:06.897' vs '1:06.896')."""
    secs_a = lap_time_to_seconds(a)
    secs_b = lap_time_to_seconds(b)
    if secs_a is None or secs_b is None:
        return False
    return abs(secs_a - secs_b) <= tolerance


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _strip_name_suffix(tokens):
    """Drop a trailing generational suffix, so 'Jerry Morlewski, Jr' and
    'Jerry Morlewski' compare on the surname rather than on 'jr'."""
    if len(tokens) > 2 and tokens[-1] in _NAME_SUFFIXES:
        return tokens[:-1]
    return tokens


def driver_names_match(a, b):
    """True if two driver names plausibly refer to the same person, tolerating
    a missing middle name/initial (e.g. curated 'Andrew Abbott' vs raw
    announcer 'Andrew T Abbott') that exact-string matching misses.

    Reuses lap_analysis.normalize_name for case/punctuation normalization,
    then compares full normalized equality first and falls back to matching
    on first + last token only, so an extra/missing middle token doesn't
    prevent a match while still requiring both ends of the name to agree.
    """
    tokens_a = _strip_name_suffix(normalize_name(a).split())
    tokens_b = _strip_name_suffix(normalize_name(b).split())
    if not tokens_a or not tokens_b:
        return False
    if tokens_a == tokens_b:
        return True
    return tokens_a[0] == tokens_b[0] and tokens_a[-1] == tokens_b[-1]


def records_match_normalized(class_a, lap_a, name_a, class_b, lap_b, name_b):
    """Normalized equivalent of comparing two (class, lapTime, driverName)
    identities -- classification still compares exact (it's already resolved
    through the org's alias map by this point), lap time and driver name use
    the tolerant comparisons above."""
    if normalize_identity_part(class_a) != normalize_identity_part(class_b):
        return False
    return lap_times_match(lap_a, lap_b) and driver_names_match(name_a, name_b)


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _find_by_identity(records, identity):
    norm_identity = tuple(normalize_identity_part(x) for x in identity)
    return next(
        (r for r in records
         if rejected_key(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"), r.get("date")) == norm_identity),
        None,
    )


def add_curated_record(p, fields):
    """Manually add a curated record (not derived from a scan candidate).

    `fields` is a dict with classAbbreviation/lapTime/driverName/marque/date.
    Returns the saved record, or None if a required field is missing.
    """
    record = {
        "classAbbreviation": (fields.get("classAbbreviation") or "").strip(),
        "lapTime": (fields.get("lapTime") or "").strip(),
        "driverName": (fields.get("driverName") or "").strip(),
        "marque": (fields.get("marque") or "").strip() or None,
        "date": (fields.get("date") or "").strip(),
        "addedAt": _utc_now_iso(),
        "source": "manual",
    }
    if not record["classAbbreviation"] or not record["lapTime"] or not record["date"]:
        return None

    curated = load_curated(p)
    curated.setdefault("records", []).append(record)
    curated["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_curated(p, curated)
    return record


def edit_curated_record(p, orig_identity, fields):
    """Update an existing curated record's fields, matched by its original
    identity (since editing changes the very fields used to identify it).

    Speedhive-sourced records get flagged `modified` so it's visible a human
    has since corrected announcer-derived data; manual records don't need the
    flag, they're already fully human-owned. Each edit that actually changes
    a field also appends an entry to `edit_history`, so the full sequence of
    human corrections is visible later, not just the fact that one happened.

    Returns the updated record, or None if not found / a required field is missing.
    """
    updated = {
        "classAbbreviation": (fields.get("classAbbreviation") or "").strip(),
        "lapTime": (fields.get("lapTime") or "").strip(),
        "driverName": (fields.get("driverName") or "").strip(),
        "marque": (fields.get("marque") or "").strip() or None,
        "date": (fields.get("date") or "").strip(),
    }
    if not updated["classAbbreviation"] or not updated["lapTime"] or not updated["date"]:
        return None

    curated = load_curated(p)
    target = _find_by_identity(curated.get("records", []), orig_identity)
    if target is None:
        return None

    changed_fields = {
        field: {"from": target.get(field), "to": new_value}
        for field, new_value in updated.items()
        if target.get(field) != new_value
    }

    target.update(updated)
    if target.get("source") != "manual":
        target["modified"] = True
        target["modified_at"] = _utc_now_iso()
        if changed_fields:
            target.setdefault("edit_history", []).append({
                "changed_at": target["modified_at"],
                "fields": changed_fields,
            })

    curated["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_curated(p, curated)
    return target


def dedupe_curated_speedhive_additions(p):
    """Remove curated rows that duplicate an already-curated (class, lap
    time, driver) record -- same real record, just computed with a
    different date, lap-time format (e.g. curated "0:59.439" vs raw
    announcer "59.439"), or missing middle name/initial than the
    pre-existing entry (see run_sync_and_diff's identity match, which used
    to compare date/lap-time/driver-name as exact strings and so treated
    these as new -- see also NEXT_SESSION_PLAN.md item 1).

    Rows are bucketed by class (already resolved through the org's alias
    map by this point, so exact class equality is fine) and compared
    pairwise within each bucket via records_match_normalized. Only a
    `source == "speedhive"` row is ever removed, and only when a
    pre-existing (non-"speedhive") row in the same match-group already
    covers it; the pre-existing row always wins and is kept as-is. Groups
    where every entry is pre-existing are left untouched -- that's a
    separate, longer-standing data question, not this specific bug.

    Returns {"removed": int, "removed_records": [dict, ...]}.
    """
    curated = load_curated(p)
    records = curated.get("records", [])

    by_class = defaultdict(list)
    for r in records:
        by_class[normalize_identity_part(r.get("classAbbreviation"))].append(r)

    to_remove = []
    for rows in by_class.values():
        assigned = [False] * len(rows)
        for i in range(len(rows)):
            if assigned[i]:
                continue
            group = [rows[i]]
            assigned[i] = True
            for j in range(i + 1, len(rows)):
                if assigned[j]:
                    continue
                if records_match_normalized(
                    rows[i].get("classAbbreviation"), rows[i].get("lapTime"), rows[i].get("driverName"),
                    rows[j].get("classAbbreviation"), rows[j].get("lapTime"), rows[j].get("driverName"),
                ):
                    group.append(rows[j])
                    assigned[j] = True
            if len(group) < 2:
                continue
            pre_existing = [r for r in group if r.get("source") != "speedhive"]
            added_via_speedhive = [r for r in group if r.get("source") == "speedhive"]
            if pre_existing and added_via_speedhive:
                to_remove.extend(added_via_speedhive)

    if to_remove:
        remove_ids = {id(r) for r in to_remove}
        curated["records"] = [r for r in records if id(r) not in remove_ids]
        curated["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        save_curated(p, curated)

    return {"removed": len(to_remove), "removed_records": to_remove}


def approve_all_candidates(p, only_action="new_record"):
    """Approve every pending candidate matching `only_action` straight into
    curated, using each candidate's own proposed values as-is.

    All pending candidates have action "new_record" -- anything the parser
    flagged as unsure (ambiguous classification, low-confidence LLM
    extraction) is routed straight to rejected by run_sync_and_diff and never
    reaches this queue.

    Returns {"approved": int, "skipped": int} (skipped = candidates left
    behind that didn't match only_action).
    """
    payload = load_candidates(p)
    candidates = payload.get("candidates", [])
    to_approve = [c for c in candidates if c.get("action") == only_action]
    remaining = [c for c in candidates if c.get("action") != only_action]

    if to_approve:
        curated = load_curated(p)
        for c in to_approve:
            proposed = c.get("proposed", {})
            curated.setdefault("records", []).append({
                "classAbbreviation": proposed.get("classAbbreviation"),
                "lapTime": proposed.get("lapTime"),
                "driverName": proposed.get("driverName"),
                "marque": proposed.get("marque"),
                "date": proposed.get("date"),
                "addedAt": _utc_now_iso(),
                "source": "speedhive",
            })
        curated["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        save_curated(p, curated)

    payload["candidates"] = remaining
    save_candidates(p, payload)
    return {"approved": len(to_approve), "skipped": len(remaining)}


def delete_curated_record(p, identity):
    """Remove a record from the curated list by identity.

    Manually-added records are permanently deleted -- there's nothing a scan
    could ever regenerate for them. Everything else is grounded in real
    announcer data, so it's blocked via the reject list by identity only
    (like any other rejection) -- deleting is a real deletion, not an
    archive: the record's edit history and any other accumulated state is
    gone with it. `restore_rejected_record` doesn't hand it straight back;
    it re-runs the scan/diff and lets the record re-earn its way back into
    the review queue, same as restoring a rejected candidate.

    Returns {"found": False} or {"found": True, "permanent": bool, "record": dict}.
    """
    curated = load_curated(p)
    target = _find_by_identity(curated.get("records", []), identity)
    if target is None:
        return {"found": False}

    curated["records"] = [r for r in curated.get("records", []) if r is not target]
    curated["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    save_curated(p, curated)

    if target.get("source") == "manual":
        return {"found": True, "permanent": True, "record": target}

    rejected_payload = load_rejected(p)
    rejected_payload.setdefault("rejected", []).append({
        "classAbbreviation": identity[0],
        "lapTime": identity[1],
        "driverName": identity[2],
        "date": identity[3],
        "rejected_at": _utc_now_iso(),
        "reason": "deleted_from_curated",
    })
    save_rejected(p, rejected_payload)
    return {"found": True, "permanent": False, "record": target}


def restore_rejected_record(org_id, storage, track_records_root, identity, bulk_parser=None):
    """Undo a rejection/deletion by identity: unblock it and let a fresh
    scan decide whether it still qualifies as a candidate. Applies equally
    whether the rejection came from deleting a curated record or rejecting
    a review-queue candidate -- either way, restoring sends it back through
    the review queue rather than handing it straight back into curated.

    Returns a dict describing the outcome for the caller to build a notice
    from: {"found": False}, or {"found": True, "reappeared": Optional[bool],
    "rescan_error": Optional[str]}.

    Pass the org's `bulk_parser` when it uses the LLM parser: the rescan
    otherwise falls back to regex, which throws away the LLM parse cache and
    can reject rows the LLM parsed fine.
    """
    p = paths_for_org(track_records_root, org_id)
    rejected_payload = load_rejected(p)
    all_rejected = rejected_payload.get("rejected", [])
    restored_entry = _find_by_identity(all_rejected, identity)
    if restored_entry is None:
        return {"found": False}

    rejected_payload["rejected"] = [r for r in all_rejected if r is not restored_entry]
    save_rejected(p, rejected_payload)

    try:
        run_sync_and_diff(org_id, storage, track_records_root, bulk_parser=bulk_parser)
        reappeared = any(
            c.get("proposed", {}).get("classAbbreviation") == identity[0]
            and c.get("proposed", {}).get("lapTime") == identity[1]
            and c.get("proposed", {}).get("driverName") == identity[2]
            for c in load_candidates(p).get("candidates", [])
        )
        return {"found": True, "reappeared": reappeared}
    except Exception as exc:
        return {"found": True, "reappeared": None, "rescan_error": str(exc)}


_online_status_cache = {}


def get_cache_status(org_id, storage, track_records_root, client=None):
    """Freshness info for the Speedhive cache -- queries Speedhive dynamically if client is provided."""
    p = paths_for_org(track_records_root, org_id)

    candidates_payload = load_candidates(p)
    pending_candidates = len(candidates_payload.get("candidates", []))

    stale_after_hours = DEFAULT_STALE_AFTER_HOURS

    last_refresh_at = None
    age_hours = None
    needs_sync_local = True

    try:
        state = storage.get_org_status(org_id) or {}
        last_refresh_at = state.get("last_refresh_at")
        if last_refresh_at:
            last_dt = datetime.fromisoformat(str(last_refresh_at).replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            needs_sync_local = age_hours >= stale_after_hours
    except Exception:
        pass

    # Check if we have a fresh cached result (within 5 minutes)
    now = time.time()
    if org_id in _online_status_cache:
        cached_time, cached_needs_sync, cached_check_source = _online_status_cache[org_id]
        if now - cached_time < 300:  # 5 minutes
            return {
                "org_id": org_id,
                "last_synced_at": last_refresh_at,
                "age_hours": age_hours,
                "needs_sync": cached_needs_sync,
                "stale_after_hours": stale_after_hours,
                "pending_candidates": pending_candidates,
                "check_source": f"{cached_check_source} (cached)"
            }

    needs_sync = needs_sync_local
    check_source = "local_age"

    if client:
        try:
            # Query Speedhive's latest 5 events for this organization
            online_events = client.get_events(org_id, limit=5) or []
            if online_events:
                cached_events = storage.get_events(org_id).payload or []
                cached_ids = {e.get("id") for e in cached_events if e.get("id")}

                # 1. Check for new event IDs not present in cache
                has_new_events = False
                for event in online_events:
                    eid = event.get("id")
                    if eid and eid not in cached_ids:
                        has_new_events = True
                        break

                if has_new_events:
                    needs_sync = True
                    check_source = "new_events_found"
                else:
                    # 2. Check if any online event has been updated since our last sync
                    if last_refresh_at:
                        last_dt = datetime.fromisoformat(str(last_refresh_at).replace("Z", "+00:00"))
                        has_updates = False
                        for event in online_events:
                            updated_at = event.get("updatedAt")
                            if updated_at:
                                try:
                                    updated_dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                                    # Speedhive sends updatedAt without an offset; comparing
                                    # that naive value with last_dt raised TypeError, which the
                                    # except below swallowed, so updates were never seen.
                                    if updated_dt.tzinfo is None:
                                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                                    if updated_dt > last_dt:
                                        has_updates = True
                                        break
                                except Exception:
                                    pass

                        if has_updates:
                            needs_sync = True
                            check_source = "event_updates_found"
                        else:
                            # Local cache matches Speedhive latest events perfectly!
                            needs_sync = False
                            check_source = "online_match"
                    else:
                        needs_sync = True
                        check_source = "never_synced"
            else:
                needs_sync = False
                check_source = "no_online_events"

            # Cache the status check
            _online_status_cache[org_id] = (now, needs_sync, check_source)

        except Exception as exc:
            print(f"[StatusCheck] Failed to fetch online status for Org {org_id}: {str(exc)}", file=sys.stderr)
            check_source = f"local_age_fallback ({str(exc)})"

    return {
        "org_id": org_id,
        "last_synced_at": last_refresh_at,
        "age_hours": age_hours,
        "needs_sync": needs_sync,
        "stale_after_hours": stale_after_hours,
        "pending_candidates": pending_candidates,
        "check_source": check_source
    }


def run_sync_and_diff(org_id, storage, track_records_root, progress_cb=None, parser=None, bulk_parser=None):
    """Extract + normalize + diff for one org, against the already-synced cache
    in storage. Does NOT perform the Speedhive sync itself -- callers are
    responsible for refreshing storage first if needed (e.g. via the generic
    refresh_org_cache machinery). Returns a summary dict.

    `parser`/`bulk_parser` are passed through to storage.get_track_records() --
    pass an LLM-based parser to use instead of the default regex parser.
    `bulk_parser` takes priority when both are given.
    """
    def report(phase):
        if progress_cb:
            progress_cb(phase)

    p = paths_for_org(track_records_root, org_id)
    if not storage.org_has_sessions(org_id):
        raise RuntimeError(f"No cache for org {org_id}; sync the org first.")

    # Cache announcement parse results (record-or-None) across scans so only
    # genuinely new announcements are ever sent through the LLM/regex parser --
    # re-diffing already-seen ones (e.g. after restoring a rejected candidate)
    # still works exactly as before since cached results still feed raw_records.
    report("Extracting announcer-flagged records")
    engine = "llm" if bulk_parser is not None else "regex"
    cache_doc = load_parse_cache(p)
    parse_cache = cache_doc.get("cache", {}) if cache_doc.get("engine") == engine else {}
    new_parse_results = {}
    raw_records = storage.get_track_records(
        org_id,
        parser=parser,
        bulk_parser=bulk_parser,
        parse_cache=parse_cache,
        on_parsed=lambda key, result: new_parse_results.__setitem__(key, result),
    )

    report("Normalizing and diffing against curated records")
    alias_map = load_json(p["alias_map"], {"aliases": {}, "always_review": []})
    curated = load_curated(p)
    rejected_rows = load_rejected(p).get("rejected", [])
    rejected_keys = {
        rejected_key(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"), r.get("date"))
        for r in rejected_rows
    }

    curated_fastest = build_curated_fastest_index(curated)
    # For "is this raw record already represented" purposes, identity is
    # (class, lap time, driver) -- NOT date. The same announcement can end up
    # with different computed dates depending on where the date comes from
    # (the announcement's own timestamp vs. an event's overall start date --
    # older imports used the latter), so requiring an exact date match let the
    # same real record look "new" and get proposed as a duplicate.
    curated_ldc = {
        make_ldc(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"))
        for r in curated.get("records", [])
    }
    rejected_ldc = {
        make_ldc(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"))
        for r in rejected_rows
    }

    # Fuzzy fallback index (class -> [(lapTime, driverName), ...]), consulted
    # only when the fast exact-match ldc sets above miss. Catches the same
    # real record surfacing again under a different lap-time format (e.g.
    # "0:59.439" vs "59.439") or a missing middle name/initial, which would
    # otherwise get proposed as a false "new record" candidate. Grows as
    # candidates are accepted below so duplicates within the same scan are
    # also caught. See NEXT_SESSION_PLAN.md item 1.
    fuzzy_index = defaultdict(list)
    for r in curated.get("records", []):
        fuzzy_index[normalize_identity_part(r.get("classAbbreviation"))].append(
            (r.get("lapTime"), r.get("driverName"))
        )
    for r in rejected_rows:
        fuzzy_index[normalize_identity_part(r.get("classAbbreviation"))].append(
            (r.get("lapTime"), r.get("driverName"))
        )

    auto_rejected = []
    seen_flagged_keys = set()
    candidates = []
    seen_candidate_ldc = set()

    # Records are a historical ledger, not "whoever is fastest right now" --
    # every announcer-flagged lap that set a record at the time it happened
    # belongs in curated eventually. So each raw row is checked individually
    # against what's already curated/rejected; a row is a candidate whenever
    # it isn't represented yet, regardless of whether some *other*, faster
    # row already exists for the same class. (Comparing only the single
    # fastest raw row per class against the current curated best -- the old
    # behavior -- meant any non-fastest historical row could never surface:
    # deleting it left no way for a scan to ever bring it back.)
    for row in raw_records:
        status, resolved = normalize_classification(row.get("classification"), alias_map)
        ts = row.get("timestamp")
        date_str = str(ts)[:10] if ts else None

        # Anything the parser itself flagged as unreliable -- an
        # unrecognized/ambiguous classification token, or (for the LLM
        # parser) a low-confidence extraction -- skips human review
        # entirely and goes straight to rejected. It's still visible and
        # restorable from the Rejected tab if that turns out to be wrong;
        # it just isn't presented as something to decide on.
        #
        # A lap announced outside a race (qualifying, practice, a grid sheet)
        # is never a track record, so it goes the same way.
        if not is_race_session(row.get("session_name"), row.get("session_type")):
            reason = "not_a_race_session"
        elif status != "ok":
            reason = status
        elif row.get("llm_uncertain"):
            reason = "llm_uncertain"
        else:
            reason = None
        if reason:
            key = rejected_key(row.get("classification"), row.get("lap_time"), row.get("driver"), date_str)
            if key in rejected_keys or key in seen_flagged_keys:
                continue
            seen_flagged_keys.add(key)
            rejected_keys.add(key)
            auto_rejected.append({
                "classAbbreviation": row.get("classification"),
                "lapTime": row.get("lap_time"),
                "driverName": row.get("driver"),
                "marque": row.get("marque"),
                "date": date_str,
                "rejected_at": _utc_now_iso(),
                "reason": reason,
            })
            continue

        secs = row.get("lap_time_seconds")
        if secs is None:
            secs = lap_time_to_seconds(row.get("lap_time"))
        if secs is None:
            continue

        proposed = {
            "classAbbreviation": resolved,
            "lapTime": row.get("lap_time"),
            "driverName": row.get("driver"),
            "marque": row.get("marque"),
            "date": date_str,
        }
        ldc = make_ldc(resolved, proposed["lapTime"], proposed["driverName"])
        if ldc in curated_ldc or ldc in rejected_ldc or ldc in seen_candidate_ldc:
            continue

        cls_key = normalize_identity_part(resolved)
        if any(
            lap_times_match(proposed["lapTime"], other_lap) and driver_names_match(proposed["driverName"], other_name)
            for other_lap, other_name in fuzzy_index[cls_key]
        ):
            continue

        seen_candidate_ldc.add(ldc)
        fuzzy_index[cls_key].append((proposed["lapTime"], proposed["driverName"]))

        current = curated_fastest.get(resolved)
        current_public = None
        if current is not None:
            current_public = {k: current[k] for k in ("classAbbreviation", "lapTime", "driverName", "marque", "date")}

        candidates.append({
            "action": "new_record",
            "classAbbreviation": resolved,
            "current": current_public,
            "proposed": proposed,
            "raw": {
                "event_name": row.get("event_name"),
                "session_name": row.get("session_name"),
                "text": row.get("text"),
            },
        })

    payload = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "org_id": org_id,
        "candidates": candidates,
    }
    # Carry the notification de-dup marker across rescans, or every rescan
    # (one runs after each curated edit) re-sends the same candidates email.
    previous_fingerprint = load_candidates(p).get("last_notified_fingerprint")
    if previous_fingerprint is not None:
        payload["last_notified_fingerprint"] = previous_fingerprint
    save_candidates(p, payload)

    if auto_rejected:
        rejected_payload = load_rejected(p)
        rejected_payload.setdefault("rejected", []).extend(auto_rejected)
        save_rejected(p, rejected_payload)

    # Persist newly-parsed announcements last, only after candidates/rejected
    # have been successfully saved -- if anything above raised, nothing here
    # gets marked cached, so a retry just redoes the same (small) amount of
    # work rather than silently losing a result.
    if new_parse_results:
        parse_cache.update(new_parse_results)
        save_parse_cache(p, {"engine": engine, "cache": parse_cache})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_ndjson(p["history"] / f"candidates_{stamp}.ndjson", payload, "candidates")

    new_count = len(candidates)
    unmapped_count = len(auto_rejected)

    if candidates:
        notify_gotify(
            f"Track records: new candidates for org {org_id}",
            f"{new_count} new record candidate(s) waiting for review at /org/{org_id}/track-records/review"
            + (f" ({unmapped_count} additional record(s) auto-rejected as unsure -- see the Rejected tab)." if unmapped_count else "."),
        )

    report("Done")
    return {
        "raw_records_scanned": len(raw_records),
        "candidates_found": len(candidates),
        "new_record_candidates": new_count,
        "unmapped_candidates": unmapped_count,
        "generated_at": payload["generated_at"],
    }


def refresh_and_scan(
    org_id,
    client,
    storage,
    track_records_root,
    *,
    mode: str = "incremental",
    force: bool = False,
    max_events=None,
    recent_backfill_events: int = 20,
    cleanup_on_full: bool = True,
    progress_cb=None,
    parser=None,
    bulk_parser=None,
):
    """Refresh the org cache if needed, then run the track-record scan.

    This is the single orchestration entrypoint used by the UI and CLI when
    they want the full update flow instead of only diffing an already-synced
    cache. `parser`/`bulk_parser` are passed through to run_sync_and_diff().
    """
    def report(phase):
        if progress_cb:
            progress_cb(phase)

    status = get_cache_status(org_id, storage, track_records_root, client=client)
    refresh_result = None
    if force or status["needs_sync"]:
        report("Refreshing Speedhive cache")
        refresh_result = refresh_org_cache(
            client=client,
            org_id=org_id,
            mode=mode,
            max_events=max_events,
            recent_backfill_events=recent_backfill_events if mode != "full" else 0,
            cleanup_on_full=cleanup_on_full,
            storage=storage,
        )
    else:
        age_str = f"{status['age_hours']:.1f}h" if status.get("age_hours") is not None else "unknown age"
        report(f"Speedhive cache is fresh ({age_str} old), skipping refresh")

    report("Scanning cached Speedhive data for track records")
    scan_result = run_sync_and_diff(org_id, storage, track_records_root, progress_cb=progress_cb, parser=parser, bulk_parser=bulk_parser)
    return {
        "status": status,
        "refresh": refresh_result,
        "scan": scan_result,
        "refresh_ran": refresh_result is not None,
    }
