from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _json_loads(payload: Optional[str]) -> Any:
    if not payload:
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


def _announcement_scan_key(session_id: Any, text: str) -> str:
    """Stable content-based identity for one announcement, used to cache
    parse results across scans. Content-based (not position/count-based) so
    it's unaffected by whether an existing session's announcement list is
    ever reordered or has entries inserted rather than only appended."""
    return hashlib.sha1(f"{session_id}:{text}".encode("utf-8")).hexdigest()


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _extract_location_parts(payload: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    address = payload.get("address")
    if isinstance(address, dict):
        city = _first_non_empty(address.get("city"), address.get("town"))
        country = _first_non_empty(address.get("country"), address.get("countryCode"))
        return city, country
    return None, None


def _extract_event_name(payload: Dict[str, Any]) -> Optional[str]:
    return _first_non_empty(payload.get("name"), payload.get("eventName"), payload.get("title"))


def _extract_event_start(payload: Dict[str, Any]) -> Optional[str]:
    return _first_non_empty(
        payload.get("startDate"),
        payload.get("startTime"),
        payload.get("date"),
        payload.get("eventDate"),
    )


def _extract_session_type(payload: Dict[str, Any]) -> Optional[str]:
    return _first_non_empty(payload.get("type"), payload.get("sessionType"), payload.get("raceType"))


@dataclass(frozen=True)
class CachedRecord:
    payload: Any
    saved_at: Optional[str]


class SpeedhiveStorage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    org_id INTEGER PRIMARY KEY,
                    name TEXT,
                    city TEXT,
                    country TEXT,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS org_championships (
                    org_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS org_events (
                    org_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY,
                    org_id INTEGER,
                    name TEXT,
                    starts_at TEXT,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_sessions (
                    event_id INTEGER PRIMARY KEY,
                    org_id INTEGER,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id INTEGER PRIMARY KEY,
                    event_id INTEGER,
                    org_id INTEGER,
                    name TEXT,
                    session_type TEXT,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_results (
                    session_id INTEGER PRIMARY KEY,
                    event_id INTEGER,
                    org_id INTEGER,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_announcements (
                    session_id INTEGER PRIMARY KEY,
                    event_id INTEGER,
                    org_id INTEGER,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_laps (
                    session_id INTEGER PRIMARY KEY,
                    event_id INTEGER,
                    org_id INTEGER,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_lap_chart (
                    session_id INTEGER PRIMARY KEY,
                    event_id INTEGER,
                    org_id INTEGER,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS org_refresh_state (
                    org_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    last_refresh_at TEXT,
                    last_full_refresh_at TEXT,
                    last_incremental_refresh_at TEXT,
                    last_refresh_mode TEXT,
                    events_cached INTEGER,
                    sessions_cached INTEGER,
                    championships_cached INTEGER,
                    new_events_detected INTEGER,
                    refreshed_events INTEGER,
                    refreshed_sessions INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_events_org_start ON events(org_id, starts_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_org_event ON sessions(org_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_type ON sessions(org_id, session_type);
                """
            )

    def _fetch_single(self, table: str, key_column: str, key_value: int) -> CachedRecord:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT payload, saved_at FROM {table} WHERE {key_column} = ?",
                (int(key_value),),
            ).fetchone()
        if row is None:
            return CachedRecord(payload=None, saved_at=None)
        return CachedRecord(payload=_json_loads(row["payload"]), saved_at=row["saved_at"])

    def _upsert_single(
        self,
        conn: sqlite3.Connection,
        table: str,
        key_column: str,
        key_value: int,
        payload: Any,
        saved_at: Optional[str],
        extra_columns: Optional[Dict[str, Any]] = None,
    ) -> None:
        saved_at = saved_at or _utc_now_iso()
        columns = [key_column, "payload", "saved_at"]
        values = [int(key_value), _json_dumps(payload), saved_at]
        assignments = ["payload = excluded.payload", "saved_at = excluded.saved_at"]
        if extra_columns:
            for column, value in extra_columns.items():
                columns.append(column)
                values.append(value)
                assignments.append(f"{column} = excluded.{column}")

        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['?'] * len(columns))}) "
            f"ON CONFLICT({key_column}) DO UPDATE SET {', '.join(assignments)}"
        )
        conn.execute(sql, values)

    def get_organization(self, org_id: int) -> CachedRecord:
        return self._fetch_single("organizations", "org_id", org_id)

    def save_organization(
        self,
        org_id: int,
        payload: Dict[str, Any],
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        city, country = _extract_location_parts(payload if isinstance(payload, dict) else {})
        name = None
        if isinstance(payload, dict):
            name = _first_non_empty(payload.get("name"), payload.get("title"))
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_organization(org_id, payload, saved_at=saved_at, conn=local_conn)
            return
        self._upsert_single(
            conn,
            "organizations",
            "org_id",
            org_id,
            payload,
            saved_at,
            extra_columns={"name": name, "city": city, "country": country},
        )

    def list_organizations(self) -> list[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT org_id, name, city, country, saved_at FROM organizations ORDER BY COALESCE(name, '') COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_championships(self, org_id: int) -> CachedRecord:
        return self._fetch_single("org_championships", "org_id", org_id)

    def save_championships(
        self,
        org_id: int,
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_championships(org_id, payload, saved_at=saved_at, conn=local_conn)
            return
        self._upsert_single(conn, "org_championships", "org_id", org_id, payload, saved_at)

    def get_events(self, org_id: int) -> CachedRecord:
        return self._fetch_single("org_events", "org_id", org_id)

    def save_events(
        self,
        org_id: int,
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_events(org_id, payload, saved_at=saved_at, conn=local_conn)
            return

        self._upsert_single(conn, "org_events", "org_id", org_id, payload, saved_at)
        if isinstance(payload, list):
            for event in payload:
                if not isinstance(event, dict):
                    continue
                event_id = _safe_int(event.get("id"))
                if event_id is None:
                    continue
                self.save_event(event_id, org_id, event, saved_at=saved_at, conn=conn)

    def get_event(self, event_id: int) -> CachedRecord:
        return self._fetch_single("events", "event_id", event_id)

    def save_event(
        self,
        event_id: int,
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_event(event_id, org_id, payload, saved_at=saved_at, conn=local_conn)
            return

        event_payload = payload if isinstance(payload, dict) else {}
        self._upsert_single(
            conn,
            "events",
            "event_id",
            event_id,
            payload,
            saved_at,
            extra_columns={
                "org_id": org_id,
                "name": _extract_event_name(event_payload),
                "starts_at": _extract_event_start(event_payload),
            },
        )

    def get_event_sessions(self, event_id: int) -> CachedRecord:
        return self._fetch_single("event_sessions", "event_id", event_id)

    def save_event_sessions(
        self,
        event_id: int,
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_event_sessions(event_id, org_id, payload, saved_at=saved_at, conn=local_conn)
            return

        self._upsert_single(
            conn,
            "event_sessions",
            "event_id",
            event_id,
            payload,
            saved_at,
            extra_columns={"org_id": org_id},
        )

        if isinstance(payload, list):
            for session in payload:
                if not isinstance(session, dict):
                    continue
                session_id = _safe_int(session.get("id"))
                if session_id is None:
                    continue
                self.save_session(session_id, event_id, org_id, session, saved_at=saved_at, conn=conn)

    def get_session(self, session_id: int) -> CachedRecord:
        return self._fetch_single("sessions", "session_id", session_id)

    def save_session(
        self,
        session_id: int,
        event_id: Optional[int],
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_session(session_id, event_id, org_id, payload, saved_at=saved_at, conn=local_conn)
            return

        session_payload = payload if isinstance(payload, dict) else {}
        resolved_event_id = _safe_int(_first_non_empty(session_payload.get("eventId"), session_payload.get("event_id"), event_id))
        self._upsert_single(
            conn,
            "sessions",
            "session_id",
            session_id,
            payload,
            saved_at,
            extra_columns={
                "event_id": resolved_event_id,
                "org_id": org_id,
                "name": _first_non_empty(session_payload.get("name"), session_payload.get("sessionName")),
                "session_type": _extract_session_type(session_payload),
            },
        )

    def get_results(self, session_id: int) -> CachedRecord:
        return self._fetch_single("session_results", "session_id", session_id)

    def save_results(
        self,
        session_id: int,
        event_id: Optional[int],
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._save_session_blob("session_results", session_id, event_id, org_id, payload, saved_at=saved_at, conn=conn)

    def get_announcements(self, session_id: int) -> CachedRecord:
        return self._fetch_single("session_announcements", "session_id", session_id)

    def save_announcements(
        self,
        session_id: int,
        event_id: Optional[int],
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._save_session_blob("session_announcements", session_id, event_id, org_id, payload, saved_at=saved_at, conn=conn)

    def get_laps(self, session_id: int) -> CachedRecord:
        return self._fetch_single("session_laps", "session_id", session_id)

    def save_laps(
        self,
        session_id: int,
        event_id: Optional[int],
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._save_session_blob("session_laps", session_id, event_id, org_id, payload, saved_at=saved_at, conn=conn)

    def get_lap_chart(self, session_id: int) -> CachedRecord:
        return self._fetch_single("session_lap_chart", "session_id", session_id)

    def save_lap_chart(
        self,
        session_id: int,
        event_id: Optional[int],
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._save_session_blob("session_lap_chart", session_id, event_id, org_id, payload, saved_at=saved_at, conn=conn)

    def _save_session_blob(
        self,
        table: str,
        session_id: int,
        event_id: Optional[int],
        org_id: Optional[int],
        payload: Any,
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self._save_session_blob(table, session_id, event_id, org_id, payload, saved_at=saved_at, conn=local_conn)
            return

        self._upsert_single(
            conn,
            table,
            "session_id",
            session_id,
            payload,
            saved_at,
            extra_columns={"event_id": event_id, "org_id": org_id},
        )

    def get_refresh_state(self, org_id: int) -> CachedRecord:
        return self._fetch_single("org_refresh_state", "org_id", org_id)

    def org_has_sessions(self, org_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE org_id = ? LIMIT 1",
                (int(org_id),),
            ).fetchone()
        return row is not None

    def load_session_payloads(self, org_id: int) -> Dict[str, Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, payload FROM sessions WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            payload = _json_loads(row["payload"])
            if isinstance(payload, dict):
                out[str(int(row["session_id"]))] = payload
        return out

    def load_results_payloads(self, org_id: int) -> Dict[str, list[Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, payload FROM session_results WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
        out: Dict[str, list[Any]] = {}
        for row in rows:
            payload = _json_loads(row["payload"])
            if isinstance(payload, list):
                out[str(int(row["session_id"]))] = payload
        return out

    def load_event_payloads(self, org_id: int) -> Dict[str, Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_id, payload FROM events WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            payload = _json_loads(row["payload"])
            if isinstance(payload, dict):
                out[str(int(row["event_id"]))] = payload
        return out

    def load_laps_payloads(self, org_id: int) -> Dict[str, list[Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, payload FROM session_laps WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
        out: Dict[str, list[Any]] = {}
        for row in rows:
            payload = _json_loads(row["payload"])
            if isinstance(payload, list):
                out[str(int(row["session_id"]))] = payload
        return out

    def load_announcements_payloads(self, org_id: int) -> Dict[str, list[Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT session_id, payload FROM session_announcements WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
        out: Dict[str, list[Any]] = {}
        for row in rows:
            payload = _json_loads(row["payload"])
            if isinstance(payload, list):
                out[str(int(row["session_id"]))] = payload
        return out

    def save_refresh_state(
        self,
        org_id: int,
        payload: Dict[str, Any],
        *,
        saved_at: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        owns_conn = conn is None
        if owns_conn:
            with self.connect() as local_conn:
                self.save_refresh_state(org_id, payload, saved_at=saved_at, conn=local_conn)
            return

        payload = payload if isinstance(payload, dict) else {}
        self._upsert_single(
            conn,
            "org_refresh_state",
            "org_id",
            org_id,
            payload,
            saved_at,
            extra_columns={
                "last_refresh_at": payload.get("last_refresh_at"),
                "last_full_refresh_at": payload.get("last_full_refresh_at"),
                "last_incremental_refresh_at": payload.get("last_incremental_refresh_at"),
                "last_refresh_mode": payload.get("last_refresh_mode"),
                "events_cached": _safe_int(payload.get("events_cached")) or 0,
                "sessions_cached": _safe_int(payload.get("sessions_cached")) or 0,
                "championships_cached": _safe_int(payload.get("championships_cached")) or 0,
                "new_events_detected": _safe_int(payload.get("new_events_detected")) or 0,
                "refreshed_events": _safe_int(payload.get("refreshed_events")) or 0,
                "refreshed_sessions": _safe_int(payload.get("refreshed_sessions")) or 0,
            },
        )

    def delete_org(self, org_id: int) -> None:
        org_id = int(org_id)
        with self.connect() as conn:
            event_ids = [
                int(row["event_id"])
                for row in conn.execute("SELECT event_id FROM events WHERE org_id = ?", (org_id,)).fetchall()
            ]
            session_ids = [
                int(row["session_id"])
                for row in conn.execute("SELECT session_id FROM sessions WHERE org_id = ?", (org_id,)).fetchall()
            ]

            if event_ids:
                placeholders = ", ".join(["?"] * len(event_ids))
                conn.execute(f"DELETE FROM event_sessions WHERE event_id IN ({placeholders})", event_ids)
                conn.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids)

            if session_ids:
                placeholders = ", ".join(["?"] * len(session_ids))
                for table in ("session_results", "session_announcements", "session_laps", "session_lap_chart", "sessions"):
                    conn.execute(f"DELETE FROM {table} WHERE session_id IN ({placeholders})", session_ids)

            for table in ("session_results", "session_announcements", "session_laps", "session_lap_chart"):
                conn.execute(f"DELETE FROM {table} WHERE org_id = ?", (org_id,))
            conn.execute("DELETE FROM sessions WHERE org_id = ?", (org_id,))
            conn.execute("DELETE FROM event_sessions WHERE org_id = ?", (org_id,))
            conn.execute("DELETE FROM events WHERE org_id = ?", (org_id,))

            conn.execute("DELETE FROM organizations WHERE org_id = ?", (org_id,))
            conn.execute("DELETE FROM org_championships WHERE org_id = ?", (org_id,))
            conn.execute("DELETE FROM org_events WHERE org_id = ?", (org_id,))
            conn.execute("DELETE FROM org_refresh_state WHERE org_id = ?", (org_id,))

    def prune_org(self, org_id: int, keep_event_ids: Iterable[int], keep_session_ids: Iterable[int]) -> Tuple[int, int]:
        keep_event_ids = sorted({int(event_id) for event_id in keep_event_ids if event_id is not None})
        keep_session_ids = sorted({int(session_id) for session_id in keep_session_ids if session_id is not None})
        removed_events = 0
        removed_sessions = 0

        with self.connect() as conn:
            stale_session_rows = conn.execute(
                "SELECT session_id, event_id FROM sessions WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
            stale_session_ids = [
                int(row["session_id"])
                for row in stale_session_rows
                if int(row["session_id"]) not in keep_session_ids or int(row["event_id"]) not in keep_event_ids
            ]

            stale_event_rows = conn.execute(
                "SELECT event_id FROM events WHERE org_id = ?",
                (int(org_id),),
            ).fetchall()
            stale_event_ids = [
                int(row["event_id"])
                for row in stale_event_rows
                if int(row["event_id"]) not in keep_event_ids
            ]
            if stale_event_ids:
                placeholders = ", ".join(["?"] * len(stale_event_ids))
                conn.execute(f"DELETE FROM events WHERE event_id IN ({placeholders})", stale_event_ids)
                conn.execute(f"DELETE FROM event_sessions WHERE event_id IN ({placeholders})", stale_event_ids)
                removed_events = len(stale_event_ids)
            if stale_session_ids:
                placeholders = ", ".join(["?"] * len(stale_session_ids))
                for table in ("sessions", "session_results", "session_announcements", "session_laps", "session_lap_chart"):
                    conn.execute(f"DELETE FROM {table} WHERE session_id IN ({placeholders})", stale_session_ids)
                removed_sessions = len(stale_session_ids)

        return removed_events, removed_sessions

    def get_org_status(self, org_id: int) -> Dict[str, Any]:
        """Retrieve and compute cache status metrics & metadata for an organization."""
        refresh_rec = self.get_refresh_state(org_id)
        payload = refresh_rec.payload if isinstance(refresh_rec.payload, dict) else {}

        def parse_iso(val: Any) -> Optional[datetime]:
            if not val:
                return None
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                return None

        full_dt = parse_iso(payload.get("last_full_refresh_at"))
        incremental_dt = parse_iso(payload.get("last_incremental_refresh_at"))
        explicit_last_dt = parse_iso(payload.get("last_refresh_at"))
        last_dt = explicit_last_dt
        if not last_dt:
            candidates = [dt for dt in (full_dt, incremental_dt) if dt]
            if candidates:
                last_dt = max(candidates)

        now = datetime.now(timezone.utc)
        age = (now - last_dt).total_seconds() if last_dt else None
        
        last_refresh_at_str = last_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z") if last_dt else None
        last_full_refresh_at_str = full_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z") if full_dt else None
        last_incremental_refresh_at_str = incremental_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z") if incremental_dt else None

        events_cached = _safe_int(payload.get("events_cached")) or 0
        sessions_cached = _safe_int(payload.get("sessions_cached")) or 0
        championships_cached = _safe_int(payload.get("championships_cached")) or 0
        new_events_detected = _safe_int(payload.get("new_events_detected")) or 0
        refreshed_events = _safe_int(payload.get("refreshed_events")) or 0
        refreshed_sessions = _safe_int(payload.get("refreshed_sessions")) or 0

        # fallback checks
        if not last_refresh_at_str:
            # check from org_events table
            events_rec = self.get_events(org_id)
            if events_rec.saved_at:
                saved_dt = parse_iso(events_rec.saved_at)
                if saved_dt:
                    last_refresh_at_str = events_rec.saved_at
                    age = (now - saved_dt).total_seconds()

        return {
            "org_id": org_id,
            "last_refresh_mode": _first_non_empty(payload.get("last_refresh_mode"), "full" if full_dt else None),
            "last_refresh_at": last_refresh_at_str,
            "last_full_refresh_at": last_full_refresh_at_str,
            "last_incremental_refresh_at": last_incremental_refresh_at_str,
            "age_seconds": age,
            "age_hours": (age / 3600.0) if age is not None else None,
            "events_cached": events_cached,
            "sessions_cached": sessions_cached,
            "championships_cached": championships_cached,
            "new_events_detected": new_events_detected,
            "refreshed_events": refreshed_events,
            "refreshed_sessions": refreshed_sessions,
        }

    def get_track_records(
        self,
        org: int,
        classification: str | None = None,
        parser: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        bulk_parser: Optional[Callable[[List[str]], List[Optional[Dict[str, Any]]]]] = None,
        parse_cache: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
        on_parsed: Optional[Callable[[str, Optional[Dict[str, Any]]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Query and parse track records from storage for the specified organization.

        `parser` defaults to the regex-based parse_track_record_text, but can be
        swapped for an alternative (e.g. an LLM-based parser) with the same
        text-in/dict-or-None-out signature.

        `bulk_parser`, if given, takes priority over `parser`: it receives every
        announcement text across the whole org in one list and returns a
        position-aligned list of parsed-record-or-None, letting the caller make
        a single LLM call for the entire org instead of one call per text.

        `parse_cache`, if given, maps announcement_scan_key(session_id, text) ->
        a previously-parsed result (or None). Announcements found in the cache
        skip parser/bulk_parser entirely -- only genuinely new announcements are
        actually parsed. `on_parsed(key, result)` is called once per such new
        announcement so the caller can persist it into their cache. The returned
        records list always reflects every announcement (cached + freshly
        parsed), so callers relying on a complete history (e.g. re-diffing
        against curated/rejected) see the exact same output either way.
        """
        from speedhive.utils.lap_analysis import extract_iso_date, parse_track_record_text

        parse_fn = parser or parse_track_record_text

        if not self.org_has_sessions(org):
            return []

        session_map = self.load_session_payloads(org)
        event_map = self.load_event_payloads(org)
        announcements_map = self.load_announcements_payloads(org)

        # Collect (context, text) for every announcement first -- needed either
        # way, but only actually required up front for the bulk_parser path.
        items: List[Dict[str, Any]] = []
        for session_id, announcements in announcements_map.items():
            session_raw = session_map.get(session_id, {})
            event_id = (
                session_raw.get("event_id")
                or session_raw.get("eventId")
                or (session_raw.get("event") or {}).get("id")
            )
            event_key = str(int(event_id)) if event_id not in (None, "") else None
            event_raw = event_map.get(event_key or "", {})
            event_name = (
                (event_raw or {}).get("name")
                or (session_raw.get("event") or {}).get("name")
                or session_raw.get("event_name")
                or session_raw.get("eventName")
            )
            session_name = session_raw.get("name") or session_raw.get("sessionName")
            session_type = session_raw.get("type")

            for announcement in announcements:
                if not isinstance(announcement, dict):
                    continue
                text = announcement.get("text") or announcement.get("message") or ""
                items.append({
                    "session_id": session_id,
                    "event_id": event_id,
                    "event_name": event_name,
                    "session_name": session_name,
                    "session_type": session_type,
                    "text": text,
                    "key": _announcement_scan_key(session_id, text),
                    "timestamp": (
                        announcement.get("timestamp")
                        or announcement.get("time")
                        or extract_iso_date(session_raw)
                        or extract_iso_date(event_raw)
                    ),
                })

        parsed_list: List[Optional[Dict[str, Any]]] = [None] * len(items)
        to_parse_indices: List[int] = []
        for i, item in enumerate(items):
            if parse_cache is not None and item["key"] in parse_cache:
                parsed_list[i] = parse_cache[item["key"]]
            else:
                to_parse_indices.append(i)

        if to_parse_indices:
            to_parse_texts = [items[i]["text"] for i in to_parse_indices]
            if bulk_parser is not None:
                fresh_results = bulk_parser(to_parse_texts)
            else:
                fresh_results = [parse_fn(text) for text in to_parse_texts]
            for i, result in zip(to_parse_indices, fresh_results):
                parsed_list[i] = result
                if on_parsed is not None:
                    on_parsed(items[i]["key"], result)

        records: List[Dict[str, Any]] = []
        wanted_class = classification.upper() if classification else None

        for item, parsed in zip(items, parsed_list):
            if not parsed:
                continue
            class_name = (parsed.get("classification") or "Unknown").upper()
            if wanted_class and class_name != wanted_class:
                continue
            event_id = item["event_id"]
            records.append(
                {
                    "event_id": int(event_id) if event_id not in (None, "") else None,
                    "event_name": item["event_name"],
                    "session_id": int(item["session_id"]),
                    "session_name": item["session_name"],
                    "session_type": item["session_type"],
                    "classification": parsed.get("classification"),
                    "lap_time": parsed.get("lap_time"),
                    "lap_time_seconds": parsed.get("lap_time_seconds"),
                    "driver": parsed.get("driver"),
                    "marque": parsed.get("marque"),
                    "llm_uncertain": parsed.get("llm_uncertain"),
                    "timestamp": item["timestamp"],
                    "text": item["text"],
                }
            )

        records.sort(key=lambda row: ((row.get("classification") or "").upper(), row.get("lap_time_seconds") or float("inf")))
        return records
