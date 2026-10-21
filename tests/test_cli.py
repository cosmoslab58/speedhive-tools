import argparse
from unittest.mock import patch

from speedhive.cli.main import default_db_path, main


@patch("speedhive.cli.main._run_module_as_main")
def test_export_dump_dispatches(mock_run):
    with patch("sys.argv", ["speedhive", "export-dump", "--org", "30476"]):
        try:
            main()
        except SystemExit:
            pass
    mock_run.assert_called_once_with(
        "speedhive.exporters.export_full_dump",
        ["--org", "30476", "--output", "./output"],
    )

@patch("speedhive.cli.main._run_module_as_main")
def test_report_consistency(mock_run):
    with patch(
        "sys.argv",
        ["speedhive", "report-consistency", "--org", "30476", "--top", "5"],
    ):
        try:
            main()
        except SystemExit:
            pass
    args_list = mock_run.call_args[0][1]
    assert "--org" in args_list
    assert "30476" in args_list
    assert "--top" in args_list
    assert "5" in args_list


@patch("speedhive.cli.main._run_module_as_main")
def test_sync_org_dispatches_without_legacy_cache_root(mock_run):
    with patch(
        "sys.argv",
        [
            "speedhive",
            "sync-org",
            "--org",
            "30476",
            "--mode",
            "incremental",
            "--recent-backfill-events",
            "3",
        ],
    ):
        try:
            main()
        except SystemExit:
            pass
    mock_run.assert_called_once_with(
        "speedhive.workflows.refresh_org_cache",
        [
            "--org",
            "30476",
            "--db-path",
            str(default_db_path()),
            "--mode",
            "incremental",
            "--recent-backfill-events",
            "3",
        ],
    )


@patch("speedhive.cli.main._run_module_as_main")
def test_import_dump_dispatches(mock_run):
    with patch("sys.argv", ["speedhive", "import-dump", "--org", "30476"]):
        try:
            main()
        except SystemExit:
            pass
    mock_run.assert_called_once_with(
        "speedhive.workflows.import_sqlite_dump",
        ["--org", "30476"],
    )


def test_discovery_registers_builtin():
    from speedhive.cli.discovery import register_discovered

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    register_discovered(sub)
    choices = list(sub.choices.keys())
    assert "export-dump" in choices
    assert "sync-org" in choices
    assert "report-consistency" in choices
    assert "export-track-records" in choices


@patch("speedhive.cli.main._run_module_as_main")
def test_export_lap_records_dispatches(mock_run):
    with patch(
        "sys.argv",
        [
            "speedhive",
            "export-lap-records",
            "--org",
            "30476",
            "--max-events",
            "10",
        ],
    ):
        try:
            main()
        except SystemExit:
            pass
    mock_run.assert_called_once_with(
        "speedhive.exporters.export_lap_records",
        ["--org", "30476", "--max-events", "10"],
    )


@patch("speedhive.cli.main._run_module_as_main")
def test_export_db_dump_dispatches(mock_run):
    with patch(
        "sys.argv",
        [
            "speedhive",
            "export-db-dump",
            "--org",
            "30476",
            "--output-dir",
            "./my_dump",
            "--max-events",
            "5",
        ],
    ):
        try:
            main()
        except SystemExit:
            pass
    mock_run.assert_called_once_with(
        "speedhive.exporters.export_db_dump",
        ["--org", "30476", "--output-dir", "my_dump", "--max-events", "5"],
    )


def test_export_curated_track_records_writes_file(tmp_path):
    from speedhive.cli.main import main

    out = tmp_path / "curated.ndjson"
    with patch(
        "sys.argv",
        [
            "speedhive",
            "export-curated-track-records",
            "--org",
            "30476",
            "--track-records-root",
            str(tmp_path / "track_records"),
            "--output",
            str(out),
        ],
    ):
        try:
            main()
        except SystemExit:
            pass

    assert out.exists()


def test_import_curated_track_records_creates_curated_file(tmp_path):
    from speedhive.cli.main import main

    track_records_root = tmp_path / "track_records"
    src = tmp_path / "input.ndjson"
    src.write_text(
        '{"classAbbreviation":"FP","lapTime":"1:13.325","driverName":"Test","date":"2026-07-15"}\n',
        encoding="utf-8",
    )

    with patch(
        "sys.argv",
        [
            "speedhive",
            "import-curated-track-records",
            "--org",
            "30476",
            "--track-records-root",
            str(track_records_root),
            "--input",
            str(src),
        ],
    ):
        try:
            main()
        except SystemExit:
            pass

    assert (track_records_root / "30476" / "track_records" / "curated.ndjson").exists()


@patch("speedhive.cli.main._configure_org")
def test_configure_command_dispatches(mock_configure):
    with patch("sys.argv", ["speedhive", "configure", "--org", "30476"]):
        try:
            main()
        except SystemExit:
            pass
    assert mock_configure.called


def test_configure_org_wizard_writes_file(tmp_path, monkeypatch):
    import json as jsonlib
    from speedhive.cli.main import _configure_org

    # Mock data directory
    monkeypatch.setenv("SPEEDHIVE_DATA_DIR", str(tmp_path))

    class Args:
        org = 99999

    # Mock inputs:
    # 1. Announcer parser engine -> Enter (regex)
    # 2. Minimum laps -> "15"
    inputs = ["", "15"]
    input_iter = iter(inputs)

    with patch("builtins.input", lambda prompt: next(input_iter)):
        _configure_org(Args())

    settings_file = tmp_path / "orgs" / "99999" / "settings.json"
    assert settings_file.exists()

    with open(settings_file) as f:
        data = jsonlib.load(f)

    assert data["parsing"]["engine"] == "regex"
    assert data["stats"]["min_laps"] == 15
    # speedhive-tools has no code path that acts on notification/email
    # settings -- the wizard must not prompt for or write them.
    assert "notifications" not in data
    assert "overrides" not in data


def test_configure_org_wizard_llm_engine_prompts_for_anthropic_keys(tmp_path, monkeypatch):
    import json as jsonlib
    from speedhive.cli.main import _configure_org

    monkeypatch.setenv("SPEEDHIVE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    class Args:
        org = 88888

    # Mock inputs:
    # 1. Announcer parser engine -> "llm"
    # 2. Anthropic API Key -> "sk-ant-test-key"
    # 3. Claude Model -> "claude-sonnet-5-5"
    # 4. Minimum laps -> Enter (default 20)
    inputs = ["llm", "sk-ant-test-key", "claude-sonnet-5-5", ""]
    input_iter = iter(inputs)

    with patch("builtins.input", lambda prompt: next(input_iter)):
        _configure_org(Args())

    settings_file = tmp_path / "orgs" / "88888" / "settings.json"
    with open(settings_file) as f:
        data = jsonlib.load(f)

    assert data["parsing"]["engine"] == "llm"
    assert data["stats"]["min_laps"] == 20
    assert data["overrides"]["ANTHROPIC_API_KEY"] == "sk-ant-test-key"
    assert data["overrides"]["ANTHROPIC_MODEL"] == "claude-sonnet-5-5"
    assert "notifications" not in data




def test_configure_org_wizard_default_model_is_not_pinned(tmp_path, monkeypatch):
    import json as jsonlib
    from speedhive.cli.main import _configure_org

    monkeypatch.setenv("SPEEDHIVE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

    class Args:
        org = 88888

    # Accept the default model (Enter) -> no ANTHROPIC_MODEL override stored.
    inputs = ["llm", "sk-ant-test-key", "", ""]
    input_iter = iter(inputs)

    with patch("builtins.input", lambda prompt: next(input_iter)):
        _configure_org(Args())

    with open(tmp_path / "orgs" / "88888" / "settings.json") as f:
        data = jsonlib.load(f)

    assert data["overrides"]["ANTHROPIC_API_KEY"] == "sk-ant-test-key"
    assert "ANTHROPIC_MODEL" not in data["overrides"]
