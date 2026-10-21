import json
from types import SimpleNamespace

import pytest
from speedhive.llm import (
    DEFAULT_MODEL,
    call_claude_json,
    get_anthropic_api_key,
    get_anthropic_model,
    parse_track_records_bulk_with_claude,
)
from speedhive.storage import SpeedhiveStorage
from speedhive.utils.llm_track_records import (
    BULK_TRACK_RECORD_RESPONSE_SCHEMA,
    TRACK_RECORD_RESPONSE_SCHEMA,
    parse_track_record_text_llm,
    parse_track_record_texts_llm_bulk,
)


def _fake_call(response):
    def call(prompt, schema):
        return response
    return call


def test_get_anthropic_api_key_prefers_org_scoped_over_bare(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_88888", raising=False)

    assert get_anthropic_api_key() is None
    assert get_anthropic_api_key(org_id=88888) is None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "bare-key")
    assert get_anthropic_api_key() == "bare-key"
    assert get_anthropic_api_key(org_id=88888) == "bare-key", "falls back to the bare var when no org-scoped one is set"

    monkeypatch.setenv("ANTHROPIC_API_KEY_88888", "org-specific-key")
    assert get_anthropic_api_key(org_id=88888) == "org-specific-key", "org-scoped var takes priority"
    assert get_anthropic_api_key(org_id=99999) == "bare-key", "a different org still falls back to the bare var"
    assert get_anthropic_api_key() == "bare-key", "no org_id given -- unaffected by any org-scoped var"


def test_get_anthropic_model_prefers_org_scoped_then_global_then_default(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL_88888", raising=False)

    assert get_anthropic_model() == DEFAULT_MODEL == "claude-haiku-5-5"
    assert get_anthropic_model(org_id=88888) == DEFAULT_MODEL

    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-bare")
    assert get_anthropic_model() == "claude-bare"
    assert get_anthropic_model(org_id=88888) == "claude-bare"

    monkeypatch.setenv("ANTHROPIC_MODEL_88888", "claude-org-specific")
    assert get_anthropic_model(org_id=88888) == "claude-org-specific"
    assert get_anthropic_model(org_id=99999) == "claude-bare"


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


def _install_fake_client(monkeypatch, text, stop_reason="end_turn"):
    """Replace anthropic.Anthropic in speedhive.llm with a fake that records
    the constructor and stream kwargs and returns a canned message."""
    import speedhive.llm as llm_mod

    calls = {}
    message = SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
    )

    class FakeMessages:
        def stream(self, **kwargs):
            calls["stream"] = kwargs
            return _FakeStream(message)

    class FakeClient:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.messages = FakeMessages()

    monkeypatch.setattr(llm_mod.anthropic, "Anthropic", FakeClient)
    return calls


def test_call_claude_json_sends_strict_schema_and_parses_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    calls = _install_fake_client(monkeypatch, '{"is_record": false}')

    result = call_claude_json("prompt text", response_schema=TRACK_RECORD_RESPONSE_SCHEMA)

    assert result == {"is_record": False}
    assert calls["client"]["api_key"] == "test-key"
    sent = calls["stream"]
    assert sent["model"] == "claude-haiku-5-5"
    assert sent["messages"] == [{"role": "user", "content": "prompt text"}]
    assert sent["output_config"]["effort"] == "low"
    assert "temperature" not in sent and "thinking" not in sent
    fmt = sent["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    # The shared provider-neutral schema itself is left untouched.
    assert "additionalProperties" not in TRACK_RECORD_RESPONSE_SCHEMA


def test_call_claude_json_bulk_schema_marks_nested_objects_strict(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = _install_fake_client(monkeypatch, '{"results": []}')

    call_claude_json("p", response_schema=BULK_TRACK_RECORD_RESPONSE_SCHEMA, max_output_tokens=65536, timeout_s=600.0)

    schema = calls["stream"]["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["results"]["items"]["additionalProperties"] is False
    assert calls["stream"]["max_tokens"] == 65536
    assert calls["client"]["timeout"] == 600.0


def test_call_claude_json_org_key_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bare-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY_88888", "org-key")
    monkeypatch.setenv("ANTHROPIC_MODEL_88888", "claude-org-model")
    calls = _install_fake_client(monkeypatch, "{}")

    call_claude_json("p", response_schema={"type": "object", "properties": {}}, org_id=88888)

    assert calls["client"]["api_key"] == "org-key"
    assert calls["stream"]["model"] == "claude-org-model"


def test_call_claude_json_without_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_88888", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY_88888"):
        call_claude_json("p", org_id=88888)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_call_claude_json_unusable_stop_reason_raises(monkeypatch, stop_reason):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _install_fake_client(monkeypatch, '{"is_rec', stop_reason=stop_reason)
    with pytest.raises(RuntimeError):
        call_claude_json("p", response_schema={"type": "object", "properties": {}})


def test_parse_track_records_bulk_with_claude_end_to_end(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    payload = {"results": [{"index": 1, "classification": "FA", "lap_time": "1:01.861", "driver": "Jane Doe"}]}
    _install_fake_client(monkeypatch, json.dumps(payload))

    out = parse_track_records_bulk_with_claude(["nothing here", "New Track Record ..."])

    assert out[0] is None
    assert out[1]["classification"] == "FA"
    assert out[1]["lap_time_seconds"] == pytest.approx(61.861)
    assert out[1]["llm_uncertain"] is False


def test_parse_track_record_text_llm_found():
    result = parse_track_record_text_llm(
        "New Track Record (1:01.861) for FA by Bob.",
        _fake_call({
            "is_record": True,
            "classification": "FA",
            "lap_time": "1:01.861",
            "driver": "Bob",
            "marque": "Swift",
        }),
    )
    assert result["classification"] == "FA"
    assert result["lap_time_seconds"] == 61.861
    assert result["marque"] == "Swift"
    assert result["llm_uncertain"] is False


def test_parse_track_record_text_llm_not_a_record():
    result = parse_track_record_text_llm(
        "Checkered flag for session 3.",
        _fake_call({"is_record": False}),
    )
    assert result is None


def test_parse_track_record_text_llm_low_confidence_still_returned():
    result = parse_track_record_text_llm(
        "New record maybe? unclear.",
        _fake_call({
            "is_record": True,
            "low_confidence": True,
            "classification": "GT3",
            "lap_time": "1:10.000",
            "driver": "Someone",
        }),
    )
    assert result is not None
    assert result["llm_uncertain"] is True


def test_parse_track_record_text_llm_missing_field_is_uncertain():
    """A missing classification/lap_time is itself unreliable, even if the
    model didn't set low_confidence -- not dropped, so it's still visible
    (run_sync_and_diff routes it to rejected rather than losing it)."""
    result = parse_track_record_text_llm(
        "New Track Record for FA by Bob.",
        _fake_call({
            "is_record": True,
            "classification": "FA",
            "lap_time": "",
            "driver": "Bob",
        }),
    )
    assert result is not None
    assert result["llm_uncertain"] is True


def test_parse_track_record_text_llm_empty_text_short_circuits():
    def call(prompt, schema):
        raise AssertionError("should not call the LLM for empty text")
    assert parse_track_record_text_llm("", call) is None
    assert parse_track_record_text_llm("   ", call) is None


def test_bulk_parse_sparse_results_aligned_by_index():
    texts = [
        "New Track Record (1:01.861) for FA by Bob.",
        "Checkered flag.",
        "New Lap Record GT2: 1:10.935 by Carl.",
    ]

    def call(prompt, schema):
        assert "[0]" in prompt and "[1]" in prompt and "[2]" in prompt
        return {
            "results": [
                {"index": 0, "classification": "FA", "lap_time": "1:01.861", "driver": "Bob"},
                {"index": 2, "classification": "GT2", "lap_time": "1:10.935", "driver": "Carl"},
            ]
        }

    results = parse_track_record_texts_llm_bulk(texts, call)
    assert len(results) == 3
    assert results[1] is None
    assert results[0]["classification"] == "FA"
    assert results[2]["lap_time_seconds"] == 70.935


def test_bulk_parse_ignores_out_of_range_or_malformed_indices():
    def call(prompt, schema):
        return {"results": [
            {"index": 99, "classification": "FA", "lap_time": "1:00.000"},
            {"index": "not-an-int", "classification": "FA", "lap_time": "1:00.000"},
            "not-a-dict",
        ]}

    results = parse_track_record_texts_llm_bulk(["one text"], call)
    assert results == [None]


def test_bulk_parse_empty_input_makes_no_call():
    def call(prompt, schema):
        raise AssertionError("should not call the LLM for an empty text list")
    assert parse_track_record_texts_llm_bulk([], call) == []


def _seed_announcement(storage, org_id, session_id, event_id, texts_with_ts):
    with storage.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, event_id, org_id, name, session_type, payload, saved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, event_id, org_id, "Race 1", "race", "{}", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO events (event_id, org_id, name, starts_at, payload, saved_at) VALUES (?,?,?,?,?,?)",
            (event_id, org_id, "Event 1", "2026-01-01", "{}", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO session_announcements (session_id, event_id, org_id, payload, saved_at) VALUES (?,?,?,?,?)",
            (session_id, event_id, org_id, json.dumps(texts_with_ts), "2026-01-01T00:00:00Z"),
        )
        conn.commit()


def test_get_track_records_parse_cache_only_calls_bulk_parser_for_new_announcements(tmp_path):
    storage = SpeedhiveStorage(tmp_path / "test.db")
    org_id = 1
    _seed_announcement(storage, org_id, session_id=100, event_id=1, texts_with_ts=[
        {"text": "New Track Record (1:01.861) for FA by Bob.", "timestamp": "2026-01-01"},
        {"text": "Checkered flag.", "timestamp": "2026-01-01"},
    ])

    call_count = {"n": 0}

    def fake_bulk(texts):
        call_count["n"] += 1
        return [
            {"classification": "FA", "lap_time": "1:01.861", "lap_time_seconds": 61.861,
             "driver": "Bob", "marque": None, "llm_uncertain": False} if "FA" in t else None
            for t in texts
        ]

    cache = {}
    updates = {}
    records = storage.get_track_records(
        org_id, bulk_parser=fake_bulk, parse_cache=cache,
        on_parsed=lambda k, v: updates.update({k: v}),
    )
    assert call_count["n"] == 1
    assert len(records) == 1
    cache.update(updates)

    # Re-scanning with a warm cache and no new announcements must not call
    # the (expensive) bulk parser again, and must still return the same
    # complete result -- this is what keeps repeat scans/auto-rescans cheap.
    records_again = storage.get_track_records(
        org_id, bulk_parser=fake_bulk, parse_cache=cache, on_parsed=lambda k, v: None,
    )
    assert call_count["n"] == 1
    assert records_again == records

    # Adding one genuinely new announcement should only send that one text
    # through the bulk parser, while the cached one is served from cache.
    _seed_announcement(storage, org_id, session_id=100, event_id=1, texts_with_ts=[
        {"text": "New Track Record (1:01.861) for FA by Bob.", "timestamp": "2026-01-01"},
        {"text": "Checkered flag.", "timestamp": "2026-01-01"},
        {"text": "New Track Record (1:10.000) for GT2 by Bob FA style.", "timestamp": "2026-01-02"},
    ])
    seen_texts = []
    def counting_bulk(texts):
        seen_texts.extend(texts)
        return fake_bulk(texts)

    records_third = storage.get_track_records(
        org_id, bulk_parser=counting_bulk, parse_cache=cache, on_parsed=lambda k, v: None,
    )
    assert len(seen_texts) == 1
    assert len(records_third) == 2
