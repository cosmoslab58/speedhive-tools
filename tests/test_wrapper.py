import json
from unittest.mock import patch, MagicMock

import pytest

from speedhive.client import Client, AuthenticatedClient
from speedhive.wrapper import SpeedhiveClient, flatten_sessions


class FakeResponse:
    def __init__(self, content):
        self.content = content


@pytest.fixture
def client():
    return Client(base_url="https://api.example.com")


def test_get_events_list(client):
    events = [
        {"id": 1, "name": "Event A"},
        {"id": 2, "name": "Event B"},
    ]
    response = FakeResponse(json.dumps(events).encode())
    with patch(
        "speedhive.wrapper.get_event_list.sync_detailed",
        return_value=response,
    ) as mock_get:
        sc = SpeedhiveClient(client)
        result = sc.get_events(org_id=30476, limit=2)
        assert result == events
        mock_get.assert_called_once_with(id=30476, client=client, offset=0, count=2)


def test_get_events_dict_response(client):
    # Some APIs return a dict with 'rows'
    response = FakeResponse(json.dumps({"rows": [{"id": 1}]}).encode())
    with patch(
        "speedhive.wrapper.get_event_list.sync_detailed",
        return_value=response,
    ):
        sc = SpeedhiveClient(client)
        result = sc.get_events(org_id=30476)
        assert result == [{"id": 1}]


def test_get_sessions_with_groups(client):
    session_data = {
        "groups": [
            {
                "sessions": [
                    {"id": 10, "name": "Practice"},
                    {"id": 11, "name": "Race"},
                ]
            }
        ]
    }
    response = FakeResponse(json.dumps(session_data).encode())
    with patch(
        "speedhive.wrapper.get_session_list.sync_detailed",
        return_value=response,
    ):
        sc = SpeedhiveClient(client)
        sessions = sc.get_sessions(event_id=100)
        assert len(sessions) == 2
        assert sessions[0]["id"] == 10


def test_get_sessions_recurses_through_nested_sub_groups(client):
    # Three levels deep, with the top-level `sessions` array empty, which is
    # what the API actually answers for a multi-day event. Before this
    # recursed, everything below the first group level was silently dropped.
    session_data = {
        "sessions": [],
        "groups": [
            {
                "name": "Saturday",
                "sessions": [{"id": 10, "name": "Practice 1"}],
                "subGroups": [
                    {
                        "name": "Group A",
                        "sessions": [{"id": 11, "name": "Qualifying A"}],
                        "subGroups": [
                            {
                                "name": "Group A Splits",
                                "sessions": [{"id": 12, "name": "Race A1"}],
                            }
                        ],
                    },
                    {
                        "name": "Group B",
                        "sessions": [{"id": 13, "name": "Qualifying B"}],
                    },
                ],
            },
            {
                "name": "Sunday",
                "sessions": [{"id": 14, "name": "Race 2"}],
            },
        ],
    }
    response = FakeResponse(json.dumps(session_data).encode())
    with patch(
        "speedhive.wrapper.get_session_list.sync_detailed",
        return_value=response,
    ):
        sc = SpeedhiveClient(client)
        sessions = sc.get_sessions(event_id=100)
        assert [s["id"] for s in sessions] == [10, 11, 12, 13, 14]


def test_get_sessions_bare_list_payload(client):
    sessions_payload = [{"id": 21, "name": "Race"}, {"id": 22, "name": "Practice"}]
    response = FakeResponse(json.dumps(sessions_payload).encode())
    with patch(
        "speedhive.wrapper.get_session_list.sync_detailed",
        return_value=response,
    ):
        sc = SpeedhiveClient(client)
        assert sc.get_sessions(event_id=100) == sessions_payload


def test_get_sessions_event_with_genuinely_none(client):
    response = FakeResponse(json.dumps({"sessions": [], "groups": []}).encode())
    with patch(
        "speedhive.wrapper.get_session_list.sync_detailed",
        return_value=response,
    ):
        sc = SpeedhiveClient(client)
        assert sc.get_sessions(event_id=100) == []


def test_flatten_sessions_dedupes_sessions_repeated_across_groups():
    # A session listed both at the top and again inside a group must appear
    # once, otherwise the recursion inflates the count it was meant to fix.
    payload = {
        "sessions": [{"id": 1, "name": "Race"}],
        "groups": [
            {"sessions": [{"id": 1, "name": "Race"}, {"id": 2, "name": "Practice"}]},
            {"subGroups": [{"sessions": [{"id": 2, "name": "Practice"}]}]},
        ],
    }
    assert [s["id"] for s in flatten_sessions(payload)] == [1, 2]


def test_flatten_sessions_keeps_entries_without_an_id():
    # No id means nothing to dedupe on, so every one is kept rather than
    # collapsing them all into a single entry.
    payload = {"groups": [{"sessions": [{"name": "A"}, {"name": "B"}]}]}
    assert flatten_sessions(payload) == [{"name": "A"}, {"name": "B"}]


def test_flatten_sessions_ignores_junk():
    assert flatten_sessions(None) == []
    assert flatten_sessions("nope") == []
    assert flatten_sessions([{"id": 1}, "junk", 7]) == [{"id": 1}]
    assert flatten_sessions({"sessions": None, "groups": None, "subGroups": None}) == []
    assert flatten_sessions({"groups": ["junk", {"sessions": [{"id": 3}]}]}) == [{"id": 3}]


def test_get_laps_flatten(client):
    lap_data = [
        {
            "competitorId": 5,
            "position": 1,
            "laps": [
                {"lap": 1, "lapTime": "1:10.5", "speed": 90},
                {"lap": 2, "lapTime": "1:11.2", "speed": 89},
            ],
        }
    ]
    response = FakeResponse(json.dumps(lap_data).encode())
    with patch(
        "speedhive.wrapper.get_all_lap_times.sync_detailed",
        return_value=response,
    ):
        sc = SpeedhiveClient(client)
        laps = sc.get_laps(session_id=200, flatten=True)
        assert len(laps) == 2
        assert laps[0]["competitorId"] == 5
        assert laps[0]["lapNumber"] == 1
        assert laps[0]["lapTime"] == "1:10.5"


def test_get_track_records(client):
    from speedhive.workflows.track_records.extract import extract_records_from_api

    # Mock iter_events and get_sessions, get_announcements
    with patch.object(
        SpeedhiveClient,
        "iter_events",
        return_value=[{"id": 1, "name": "EventX"}],
    ), patch.object(
        SpeedhiveClient,
        "get_sessions",
        return_value=[{"id": 100, "name": "SessionX"}],
    ), patch.object(
        SpeedhiveClient,
        "get_announcements",
        return_value=[
            {
                "text": "New Track Record (1:17.870) for IT7 by Bob Cross.",
                "timestamp": "2025-01-01T00:00:00Z",
            }
        ],
    ), patch(
        "speedhive.workflows.track_records.extract.parse_track_record_text",
        return_value={
            "classification": "IT7",
            "lap_time": "1:17.870",
            "lap_time_seconds": 77.87,
            "driver": "Bob Cross",
            "marque": None,
        },
    ):
        sc = SpeedhiveClient(client)
        records = extract_records_from_api(sc, org_id=30476)
        assert len(records) == 1
        assert records[0]["classification"] == "IT7"


def test_create_without_token():
    sc = SpeedhiveClient.create(base_url="https://example.com", timeout=10)
    assert isinstance(sc.client, Client)
    assert sc.client.base_url == "https://example.com"


def test_create_with_token():
    sc = SpeedhiveClient.create(token="abc", base_url="https://example.com")
    assert isinstance(sc.client, AuthenticatedClient)
    assert sc.client.token == "abc"
