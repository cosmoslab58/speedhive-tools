import json

from speedhive.workflows.track_records import curation
from speedhive.stores import track_records as store
from speedhive.ndjson import dumps_ndjson, load_ndjson, parse_ndjson_lines, save_ndjson


def test_ndjson_roundtrip(tmp_path):
    doc = {"date": "2026-07-15", "records": [{"a": 1}, {"b": 2}]}
    path = tmp_path / "x.ndjson"
    save_ndjson(path, doc, "records")

    lines = path.read_text().strip().splitlines()
    assert json.loads(lines[0]) == {"_meta": {"date": "2026-07-15"}}
    assert len(lines) == 3

    loaded = load_ndjson(path, {"date": None, "records": []}, "records")
    assert loaded == doc


def test_ndjson_no_meta_line_when_no_meta():
    body = dumps_ndjson({"rejected": [{"x": 1}]}, "rejected")
    lines = body.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"x": 1}
    assert parse_ndjson_lines(lines, "rejected") == {"rejected": [{"x": 1}]}


def test_ndjson_missing_file_returns_fresh_default(tmp_path):
    default = {"date": None, "records": []}
    loaded = load_ndjson(tmp_path / "missing.ndjson", default, "records")
    loaded["records"].append({"a": 1})
    # the shared default must not be mutated
    assert default["records"] == []


def test_legacy_json_migrates_to_ndjson(tmp_path):
    """Old pretty-printed curated.json files must transparently become NDJSON."""
    legacy_doc = {"date": "2026-03-01", "records": [
        {"classAbbreviation": "FA", "lapTime": "1:01.861", "driverName": "J. Lewis Cooper, Jr", "marque": "Swift", "date": "2009-05-10"},
        {"classAbbreviation": "GT1", "lapTime": "1:05.5", "driverName": "X", "marque": None, "date": "2019-06-01"},
    ]}
    p = curation.paths_for_org(tmp_path, 999)
    p["dir"].mkdir(parents=True)
    (p["dir"] / "curated.json").write_text(json.dumps(legacy_doc))

    loaded = curation.load_curated(p)
    assert loaded["date"] == "2026-03-01"
    assert len(loaded["records"]) == 2

    # ndjson now exists, legacy renamed aside
    assert p["curated"].exists()
    assert not (p["dir"] / "curated.json").exists()
    assert (p["dir"] / "curated.json.migrated").exists()

    # round-trip through save/load preserves everything
    loaded["date"] = "2026-07-15"
    curation.save_curated(p, loaded)
    again = curation.load_curated(p)
    assert again["date"] == "2026-07-15"
    assert again["records"] == loaded["records"]


def test_track_record_store_helpers_round_trip(tmp_path):
    p = store.paths_for_org(tmp_path, 999)
    assert p["curated"].name == "curated.ndjson"
    assert p["rejected"].name == "rejected.ndjson"

    payload = {"enabled": True, "de_duplicate": False}
    config_path = p["dir"] / "config.json"
    store.save_json(config_path, payload)
    assert store.load_json(config_path, {}) == payload


def test_lap_time_to_seconds_is_strict():
    assert curation.lap_time_to_seconds("1:13.325") == 73.325
    assert curation.lap_time_to_seconds("63.004") == 63.004
    assert curation.lap_time_to_seconds("not-a-time") is None
    assert curation.lap_time_to_seconds(None) is None


def test_normalize_classification_aliases_and_review():
    alias_map = {"aliases": {"spec miata": "SM"}, "always_review": ["F5"]}
    assert curation.normalize_classification("Spec Miata", alias_map) == ("ok", "SM")
    assert curation.normalize_classification("f5", alias_map) == ("ambiguous", None)
    assert curation.normalize_classification("IT7", alias_map) == ("ok", "IT7")


def test_delete_curated_record_normalization(tmp_path):
    p = curation.paths_for_org(tmp_path, 999)
    p["dir"].mkdir(parents=True)

    # 1. Populate curated with a record that has None/""/mixed-case driverName
    curated_doc = {
        "date": "2026-07-16",
        "records": [
            {
                "classAbbreviation": "fa",
                "lapTime": "1:01.861",
                "driverName": None,
                "marque": "Swift",
                "date": "2026-07-16",
                "source": "speedhive"
            },
            {
                "classAbbreviation": "SM",
                "lapTime": "1:22.456",
                "driverName": "John Doe",
                "marque": "Mazda",
                "date": "2026-07-16",
                "source": "speedhive"
            }
        ]
    }
    curation.save_curated(p, curated_doc)

    # Delete first record using identity with empty string "" for driverName (how UI form behaves)
    identity_1 = ("FA", "1:01.861", "", "2026-07-16")
    res_1 = curation.delete_curated_record(p, identity_1)
    assert res_1["found"] is True
    assert res_1["permanent"] is False

    # Delete second record with mismatched case for driverName
    identity_2 = ("sm", "1:22.456", "JOHN DOE", "2026-07-16")
    res_2 = curation.delete_curated_record(p, identity_2)
    assert res_2["found"] is True

    # 2. Check rejected contains both
    rejected = curation.load_rejected(p).get("rejected", [])
    assert len(rejected) == 2

    # 3. Verify make_ldc behaves as expected for checks
    rejected_ldc = {
        curation.make_ldc(r.get("classAbbreviation"), r.get("lapTime"), r.get("driverName"))
        for r in rejected
    }
    raw_ldc_1 = curation.make_ldc("FA", "1:01.861", None)
    assert raw_ldc_1 in rejected_ldc

    raw_ldc_2 = curation.make_ldc("SM", "1:22.456", "john doe")
    assert raw_ldc_2 in rejected_ldc


def _seed_announcement(storage, org_id, session_id, event_id, texts_with_ts, session_payload=None):
    with storage.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, event_id, org_id, name, session_type, payload, saved_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, event_id, org_id, "Race 1", "race", json.dumps(session_payload or {}), "2026-01-01T00:00:00Z"),
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


def test_delete_curated_record_does_not_retain_full_record_copy(tmp_path):
    """A curated record's edit history (and everything else about it) must
    be genuinely gone after deletion -- only enough identity to block it
    from reappearing is kept on the reject list."""
    p = curation.paths_for_org(tmp_path, 999)
    p["dir"].mkdir(parents=True)
    curation.save_curated(p, {"date": "2026-07-16", "records": [{
        "classAbbreviation": "FA",
        "lapTime": "1:01.861",
        "driverName": "Bob",
        "marque": None,
        "date": "2026-07-16",
        "source": "speedhive",
        "modified": True,
        "edit_history": [{"changed_at": "2026-07-01T00:00:00Z", "fields": {"lapTime": {"from": "1:02.000", "to": "1:01.861"}}}],
    }]})

    identity = ("FA", "1:01.861", "Bob", "2026-07-16")
    result = curation.delete_curated_record(p, identity)
    assert result["permanent"] is False

    rejected = curation.load_rejected(p).get("rejected", [])
    assert len(rejected) == 1
    assert "record" not in rejected[0]
    assert "edit_history" not in rejected[0]
    assert rejected[0]["reason"] == "deleted_from_curated"


def test_restore_after_curated_delete_goes_to_review_queue_not_curated(tmp_path):
    """Restoring a record deleted from curated must send it back through the
    review queue -- not directly back into curated as a literal undo."""
    from speedhive.storage import SpeedhiveStorage

    org_id = 777
    storage = SpeedhiveStorage(tmp_path / "cache.db")
    p = curation.paths_for_org(tmp_path, org_id)
    p["dir"].mkdir(parents=True)

    _seed_announcement(storage, org_id, session_id=100, event_id=1, texts_with_ts=[
        {"text": "New Track Record (1:01.861) for FA by Bob.", "timestamp": "2026-07-16"},
    ])
    curation.save_curated(p, {"date": "2026-07-16", "records": [{
        "classAbbreviation": "FA",
        "lapTime": "1:01.861",
        "driverName": "Bob",
        "marque": None,
        "date": "2026-07-16",
        "source": "speedhive",
    }]})

    identity = ("FA", "1:01.861", "Bob", "2026-07-16")
    delete_result = curation.delete_curated_record(p, identity)
    assert delete_result["permanent"] is False

    # Blocked from reappearing while it's still on the reject list.
    scan_while_rejected = curation.run_sync_and_diff(org_id, storage, tmp_path)
    assert scan_while_rejected["candidates_found"] == 0

    restore_result = curation.restore_rejected_record(org_id, storage, tmp_path, identity)
    assert restore_result["found"] is True
    assert restore_result["reappeared"] is True

    assert curation.load_curated(p).get("records", []) == []
    candidates = curation.load_candidates(p).get("candidates", [])
    assert len(candidates) == 1
    assert candidates[0]["proposed"]["classAbbreviation"] == "FA"


def _seed_single_curated_record(tmp_path, org_id=999, **overrides):
    p = curation.paths_for_org(tmp_path, org_id)
    p["dir"].mkdir(parents=True)
    record = {
        "classAbbreviation": "FA",
        "lapTime": "1:02.500",
        "driverName": "Jane Doe",
        "marque": "Swift",
        "date": "2026-07-01",
        "source": "speedhive",
    }
    record.update(overrides)
    curation.save_curated(p, {"date": "2026-07-01", "records": [record]})
    return p


def test_edit_curated_record_appends_edit_history_entry(tmp_path):
    p = _seed_single_curated_record(tmp_path)
    identity = ("FA", "1:02.500", "Jane Doe", "2026-07-01")

    updated = curation.edit_curated_record(p, identity, {
        "classAbbreviation": "FA",
        "lapTime": "1:01.861",
        "driverName": "Jane Doe",
        "marque": "Swift",
        "date": "2026-07-01",
    })

    assert updated["modified"] is True
    assert len(updated["edit_history"]) == 1
    entry = updated["edit_history"][0]
    assert entry["fields"] == {"lapTime": {"from": "1:02.500", "to": "1:01.861"}}
    assert entry["changed_at"] == updated["modified_at"]


def test_edit_curated_record_sequential_edits_are_not_cumulative(tmp_path):
    p = _seed_single_curated_record(tmp_path)
    identity = ("FA", "1:02.500", "Jane Doe", "2026-07-01")

    first = curation.edit_curated_record(p, identity, {
        "classAbbreviation": "FA",
        "lapTime": "1:01.861",
        "driverName": "Jane Doe",
        "marque": "Swift",
        "date": "2026-07-01",
    })
    new_identity = ("FA", "1:01.861", "Jane Doe", "2026-07-01")
    second = curation.edit_curated_record(p, new_identity, {
        "classAbbreviation": "FA",
        "lapTime": "1:01.861",
        "driverName": "John Doe",
        "marque": "Swift",
        "date": "2026-07-01",
    })

    assert first["classAbbreviation"] == second["classAbbreviation"]  # same logical record, matched again by its new identity
    history = second["edit_history"]
    assert len(history) == 2
    assert history[0]["fields"] == {"lapTime": {"from": "1:02.500", "to": "1:01.861"}}
    assert history[1]["fields"] == {"driverName": {"from": "Jane Doe", "to": "John Doe"}}


def test_edit_curated_record_noop_edit_does_not_append_empty_entry(tmp_path):
    p = _seed_single_curated_record(tmp_path)
    identity = ("FA", "1:02.500", "Jane Doe", "2026-07-01")

    updated = curation.edit_curated_record(p, identity, {
        "classAbbreviation": "FA",
        "lapTime": "1:02.500",
        "driverName": "Jane Doe",
        "marque": "Swift",
        "date": "2026-07-01",
    })

    assert updated["modified"] is True  # unconditional flag refresh is unchanged
    assert "edit_history" not in updated


def test_edit_curated_record_manual_source_never_gets_modified_or_history(tmp_path):
    p = _seed_single_curated_record(tmp_path, source="manual")
    identity = ("FA", "1:02.500", "Jane Doe", "2026-07-01")

    updated = curation.edit_curated_record(p, identity, {
        "classAbbreviation": "FA",
        "lapTime": "1:01.861",
        "driverName": "Jane Doe",
        "marque": "Swift",
        "date": "2026-07-01",
    })

    assert "modified" not in updated
    assert "edit_history" not in updated


def test_lap_times_match_tolerates_format_and_rounding():
    # curated "0:59.439" vs raw announcer "59.439" (no leading "0:")
    assert curation.lap_times_match("0:59.439", "59.439") is True
    # hand-transcription rounding, within the 0.01s tolerance
    assert curation.lap_times_match("1:06.897", "1:06.896") is True
    # genuinely different times must not match
    assert curation.lap_times_match("1:11.501", "1:12.190") is False
    # unparseable input never matches
    assert curation.lap_times_match("not-a-time", "1:06.896") is False
    assert curation.lap_times_match(None, "1:06.896") is False


def test_driver_names_match_tolerates_missing_middle_name():
    assert curation.driver_names_match("Andrew Abbott", "Andrew Thomas Abbott") is True
    assert curation.driver_names_match("Andrew Abbott", "Andrew T Abbott") is True
    # exact match (post-normalization) still matches
    assert curation.driver_names_match("john doe", "John Doe") is True
    # different first or last name must not match
    assert curation.driver_names_match("Steve Ives", "Steven Ives") is False
    assert curation.driver_names_match("Andrew Abbott", "Andrew Smith") is False
    # empty/missing names never match
    assert curation.driver_names_match("", "Andrew Abbott") is False
    assert curation.driver_names_match(None, None) is False


def test_records_match_normalized_requires_exact_class():
    assert curation.records_match_normalized(
        "FV", "1:13.101", "Andrew Abbott",
        "FV", "1:13.099", "Andrew Thomas Abbott",
    ) is True
    # same lap/driver but a different class is not the same record
    assert curation.records_match_normalized(
        "FV", "1:13.101", "Andrew Abbott",
        "FP", "1:13.101", "Andrew Abbott",
    ) is False


def test_dedupe_curated_speedhive_additions_catches_format_variants(tmp_path):
    """The exact-string dedupe historically missed duplicates that differ
    only in lap-time format or a middle name/initial -- see
    NEXT_SESSION_PLAN.md item 1. Covers both the original exact-match case
    and the newer normalized-tolerance case in one pass."""
    p = curation.paths_for_org(tmp_path, 999)
    p["dir"].mkdir(parents=True)

    curated_doc = {
        "date": "2026-07-16",
        "records": [
            # Exact-match duplicate (different date only) -- the original bug.
            {"classAbbreviation": "GT3", "lapTime": "1:11.501", "driverName": "Paul Young",
             "marque": "Ford Probe", "date": "2020-06-28", "source": "manual"},
            {"classAbbreviation": "GT3", "lapTime": "1:11.501", "driverName": "Paul Young",
             "marque": None, "date": "2020-06-27", "source": "speedhive"},

            # Lap-time-format duplicate: "0:59.439" (curated) vs "59.439" (speedhive raw).
            {"classAbbreviation": "P1", "lapTime": "0:59.439", "driverName": "Jonathan Finstrom",
             "marque": "Staudacher S08", "date": "2021-05-27", "source": "manual"},
            {"classAbbreviation": "P1", "lapTime": "59.439", "driverName": "Jonathan Finstrom",
             "marque": None, "date": "2021-05-27", "source": "speedhive"},

            # Middle-name duplicate: curated omits the middle name the raw announcement has.
            {"classAbbreviation": "FV", "lapTime": "1:12.563", "driverName": "Andrew Abbott",
             "marque": "Vector AM-1", "date": "2019-08-04", "source": "manual"},
            {"classAbbreviation": "FV", "lapTime": "1:12.563", "driverName": "Andrew Thomas Abbott",
             "marque": None, "date": "2019-08-03", "source": "speedhive"},

            # Not a duplicate -- different class -- must survive untouched.
            {"classAbbreviation": "FP", "lapTime": "1:12.563", "driverName": "Andrew Abbott",
             "marque": None, "date": "2019-08-05", "source": "speedhive"},
        ],
    }
    curation.save_curated(p, curated_doc)

    result = curation.dedupe_curated_speedhive_additions(p)
    assert result["removed"] == 3
    assert {r["classAbbreviation"] for r in result["removed_records"]} == {"GT3", "P1", "FV"}
    assert all(r["source"] == "speedhive" for r in result["removed_records"])

    remaining = curation.load_curated(p)["records"]
    assert len(remaining) == 4
    assert {(r["classAbbreviation"], r["source"]) for r in remaining} == {
        ("GT3", "manual"), ("P1", "manual"), ("FV", "manual"), ("FP", "speedhive"),
    }



def test_is_race_session_reads_the_name_before_the_type():
    races = [
        ("Group 6 Feature Race - CFC;F5;FA;FB;FC", "race"),
        ("Saturday Class Race", "qualify"),  # older uploads mis-type races as qualify
        ("Group 1 Sunday Class Race / SCCA Qualifying - EP;FP", "race"),
        ("Group 3 Sunday Qualifying WHRRI Race - SR,SRF,SRF3", "race"),
        ("Group 8 Big Bore Bash", "race"),
        ("Group 13", "race"),  # no keywords: the type decides
        (None, None),  # nothing to go on: not dropped
    ]
    non_races = [
        ("Group 1 Qualifying - EP;FP;GTL;HP;ITA;ITS;T4", "qualify"),
        ("GRID SATURDAY RACE GROUP 5", "qualify"),  # a grid sheet holds qualifying laps
        ("Practice Starts and Races", "race"),  # drivers school, mis-typed as race
        ("Saturday BR Competition", "race"),  # bracket racing
        ("Group 2", "qualify"),
    ]
    for name, session_type in races:
        assert curation.is_race_session(name, session_type), name
    for name, session_type in non_races:
        assert not curation.is_race_session(name, session_type), name


def test_scan_auto_rejects_records_announced_outside_a_race(tmp_path):
    """Only race laps count: a record announced in qualifying goes straight to
    rejected with a reason, never into the review queue."""
    from speedhive.storage import SpeedhiveStorage

    org_id = 778
    storage = SpeedhiveStorage(tmp_path / "cache.db")
    p = curation.paths_for_org(tmp_path, org_id)
    p["dir"].mkdir(parents=True)

    _seed_announcement(storage, org_id, session_id=200, event_id=1, texts_with_ts=[
        {"text": "New Track Record (1:06.493) for F6 by Sven de Vries.", "timestamp": "2019-05-04"},
    ], session_payload={"name": "Qualifying", "type": "qualify"})
    _seed_announcement(storage, org_id, session_id=201, event_id=1, texts_with_ts=[
        {"text": "New Track Record (1:07.284) for F6 by Sven de Vries.", "timestamp": "2019-05-04"},
    ], session_payload={"name": "Saturday Race 2", "type": "race"})

    result = curation.run_sync_and_diff(org_id, storage, tmp_path)
    assert result["candidates_found"] == 1
    candidates = curation.load_candidates(p)["candidates"]
    assert candidates[0]["proposed"]["lapTime"] == "1:07.284"

    rejected = curation.load_rejected(p)["rejected"]
    assert [(r["lapTime"], r["reason"]) for r in rejected] == [("1:06.493", "not_a_race_session")]

    # A second scan doesn't reject the same lap twice.
    curation.run_sync_and_diff(org_id, storage, tmp_path)
    assert len(curation.load_rejected(p)["rejected"]) == 1


def test_driver_names_match_ignores_a_generational_suffix():
    assert curation.driver_names_match("Jerry Morlewski, Jr", "Jerry Morlewski")
    assert curation.driver_names_match("J. Lewis Cooper, Jr", "J Lewis Cooper Jr")
    assert curation.driver_names_match("John Edgell III", "John Edgell")
    assert not curation.driver_names_match("Jerry Morlewski, Jr", "Jerry Smith")


def test_rescan_keeps_the_notification_fingerprint(tmp_path):
    """Every curated edit triggers a rescan; dropping the de-dup marker there
    re-sent the same candidates email each time."""
    from speedhive.storage import SpeedhiveStorage

    org_id = 779
    storage = SpeedhiveStorage(tmp_path / "cache.db")
    p = curation.paths_for_org(tmp_path, org_id)
    p["dir"].mkdir(parents=True)
    _seed_announcement(storage, org_id, session_id=300, event_id=1, texts_with_ts=[
        {"text": "New Track Record (1:01.861) for FA by Bob.", "timestamp": "2026-07-16"},
    ])
    curation.run_sync_and_diff(org_id, storage, tmp_path)
    payload = curation.load_candidates(p)
    payload["last_notified_fingerprint"] = "fp-1"
    curation.save_candidates(p, payload)

    curation.run_sync_and_diff(org_id, storage, tmp_path)
    assert curation.load_candidates(p)["last_notified_fingerprint"] == "fp-1"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "curated.ndjson"
    store.save_curated({"dir": tmp_path, "curated": target}, {"date": "2026-01-01", "records": [{"a": 1}]})
    store.save_curated({"dir": tmp_path, "curated": target}, {"date": "2026-01-02", "records": [{"a": 2}]})
    assert [f.name for f in tmp_path.iterdir()] == ["curated.ndjson"]
    assert store.load_curated({"curated": target})["records"] == [{"a": 2}]
