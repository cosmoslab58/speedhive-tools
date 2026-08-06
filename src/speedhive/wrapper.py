"""User‑friendly wrapper around the low‑level client."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional

from attrs import define, field

from speedhive.client import Client, AuthenticatedClient
from speedhive.generated.api.system_time_controller import get_time as time_api
from speedhive.generated.api.organization_controller import get_event_list, get_organization, get_championship_list
from speedhive.generated.api.event_controller import get_event, get_session_list
from speedhive.generated.api.session_controller import (
    get_all_lap_times,
    get_classification,
    get_announcements,
    get_session,
    get_lap_chart,
)
from speedhive.generated.api.championship_controller import get_championship
from speedhive.generated.models.time import Time as TimeModel
from speedhive.utils.lap_analysis import parse_track_record_text


def flatten_sessions(payload: Any) -> List[Dict[str, Any]]:
    """Pull every session out of an event's session listing.

    Sessions usually live under ``groups[].sessions`` with the top-level array
    empty, and groups can nest arbitrarily deep through ``subGroups``. Missing a
    nested group silently loses sessions, which downstream looks identical to an
    event that genuinely had none, so this recurses through both keys.

    Some endpoints answer with a bare JSON array of sessions instead of the
    grouped object; that form is passed straight through.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    found: List[Dict[str, Any]] = []
    seen: set = set()

    def collect(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for session in node.get("sessions") or []:
            if not isinstance(session, dict):
                continue
            session_id = session.get("id")
            if session_id is not None:
                if session_id in seen:
                    continue
                seen.add(session_id)
            found.append(session)
        for group in node.get("groups") or []:
            collect(group)
        for sub_group in node.get("subGroups") or []:
            collect(sub_group)

    collect(payload)
    return found


@define
class SpeedhiveClient:
    client: Client | AuthenticatedClient = field()

    @classmethod
    def create(
        cls,
        base_url: str = "https://api2.mylaps.com",
        token: Optional[str] = None,
        timeout: float = 30.0,
        **kwargs: Any,
    ) -> "SpeedhiveClient":
        if token:
            low_client = AuthenticatedClient(
                base_url=base_url,
                token=token,
                timeout=timeout,
                **kwargs,
            )
        else:
            low_client = Client(
                base_url=base_url,
                timeout=timeout,
                **kwargs,
            )
        return cls(client=low_client)

    @staticmethod
    def _parse_response(response) -> Any:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and status_code >= 400:
            response.raise_for_status()
        if not response.content:
            return None
        try:
            return json.loads(response.content)
        except Exception:
            return None

    # Organization
    def get_organization(self, org_id: int) -> Optional[Dict[str, Any]]:
        response = get_organization.sync_detailed(id=org_id, client=self.client)
        return self._parse_response(response)

    def get_events(self, org_id: int, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        kwargs = {"id": org_id, "client": self.client, "offset": offset}
        if limit is not None:
            kwargs["count"] = limit
        response = get_event_list.sync_detailed(**kwargs)
        result = self._parse_response(response)
        if isinstance(result, dict):
            return result.get("rows", result.get("events", []))
        return result if isinstance(result, list) else []

    def iter_events(self, org_id: int, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        offset = 0
        while True:
            events = self.get_events(org_id=org_id, limit=page_size, offset=offset)
            if not events:
                break
            yield from events
            if len(events) < page_size:
                break
            offset += page_size

    # Event
    def get_event(self, event_id: int, include_sessions: bool = False) -> Optional[Dict[str, Any]]:
        response = get_event.sync_detailed(
            id=event_id, client=self.client, sessions=include_sessions
        )
        return self._parse_response(response)

    def get_sessions(self, event_id: int) -> List[Dict[str, Any]]:
        response = get_session_list.sync_detailed(id=event_id, client=self.client)
        return flatten_sessions(self._parse_response(response))

    # Session
    def get_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        response = get_session.sync_detailed(id=session_id, client=self.client)
        return self._parse_response(response)

    def get_laps(self, session_id: int, flatten: bool = True) -> List[Dict[str, Any]]:
        response = get_all_lap_times.sync_detailed(id=session_id, client=self.client)
        result = self._parse_response(response)
        if isinstance(result, dict):
            rows = result.get("rows", result.get("laps", []))
            if not flatten:
                return rows
            result = rows
        if not isinstance(result, list):
            return []
        flat = []
        for item in result:
            if isinstance(item, dict) and "laps" in item:
                for lap in item["laps"]:
                    flat.append({
                        "competitorId": item.get("competitorId") or item.get("id"),
                        "position": item.get("position"),
                        "lapNumber": lap.get("lap") or lap.get("lapNumber"),
                        "lapTime": lap.get("lapTime") or lap.get("lap_time"),
                        "speed": lap.get("speed"),
                        "inPit": lap.get("inPit"),
                        **{k: v for k, v in lap.items()
                           if k not in ("lap", "lapNumber", "lapTime", "lap_time", "speed", "inPit")}
                    })
            else:
                flat.append(item)
        return flat

    def get_results(self, session_id: int) -> List[Dict[str, Any]]:
        response = get_classification.sync_detailed(id=session_id, client=self.client)
        result = self._parse_response(response)
        if isinstance(result, dict):
            return result.get("rows", result.get("classification", []))
        return result if isinstance(result, list) else []

    def get_announcements(self, session_id: int) -> List[Dict[str, Any]]:
        response = get_announcements.sync_detailed(id=session_id, client=self.client)
        result = self._parse_response(response)
        if isinstance(result, dict):
            return result.get("announcements", result.get("rows", []))
        return result if isinstance(result, list) else []

    def get_lap_chart(self, session_id: int) -> List[Dict[str, Any]]:
        response = get_lap_chart.sync_detailed(id=session_id, client=self.client)
        result = self._parse_response(response)
        if isinstance(result, dict):
            if isinstance(result.get("rows"), list):
                return result.get("rows", [])
            if isinstance(result.get("chart"), list):
                return result.get("chart", [])
            if isinstance(result.get("positionRows"), list):
                position_rows = result.get("positionRows", [])
                num_laps = 0
                for row in position_rows:
                    if isinstance(row, list):
                        num_laps = max(num_laps, len(row))
                
                rows = []
                for lap_idx in range(num_laps):
                    positions = []
                    raw_positions = []
                    for row in position_rows:
                        if not isinstance(row, list) or lap_idx >= len(row):
                            continue
                        entry = row[lap_idx]
                        if not isinstance(entry, dict):
                            continue
                        label = entry.get("startNumber")
                        if not label and entry.get("position") is not None:
                            label = f"P{entry.get('position')}"
                        if label:
                            positions.append(str(label))
                        raw_positions.append(entry)
                    rows.append({
                        "lapNumber": lap_idx + 1,
                        "positions": positions,
                        "rawPositions": raw_positions,
                    })
                return rows
        return result if isinstance(result, list) else []

    # Championships
    def get_championships(self, org_id: int) -> List[Dict[str, Any]]:
        response = get_championship_list.sync_detailed(id=org_id, client=self.client)
        result = self._parse_response(response)
        return result if isinstance(result, list) else []

    def get_championship(self, championship_id: int) -> Optional[Dict[str, Any]]:
        response = get_championship.sync_detailed(id=championship_id, client=self.client)
        return self._parse_response(response)

    # Utility
    def get_server_time(self) -> Optional[TimeModel]:
        return time_api.sync(client=self.client)


