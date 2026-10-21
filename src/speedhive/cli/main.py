#!/usr/bin/env python3
"""Speedhive Tools CLI – unified entry point."""

import argparse
import json
import os
import sys
from pathlib import Path

from speedhive.cli.discovery import register_discovered
from speedhive.exporters.export_curated_track_records import export_curated_track_records_ndjson
from speedhive.workflows.track_records.import_curated import import_curated_track_records_ndjson
from speedhive.wrapper import SpeedhiveClient


def default_db_path() -> Path:
    db_path = os.environ.get("SPEEDHIVE_DB_PATH")
    if db_path:
        return Path(db_path)
    data_dir = os.environ.get("SPEEDHIVE_DATA_DIR", "./data")
    return Path(data_dir) / "speedhive.db"


def _run_module_as_main(module_name: str, args):
    import importlib
    mod = importlib.import_module(module_name)
    if hasattr(mod, "main") and callable(mod.main):
        return mod.main(argv=args)
    raise RuntimeError(f"Module {module_name} has no main(argv)")


def _export_full_dump(args):
    argv = ["--org", str(args.org), "--output", str(args.output)]
    if args.verbose:
        argv.append("--verbose")
    if args.no_resume:
        argv.append("--no-resume")
    if args.max_events:
        argv.extend(["--max-events", str(args.max_events)])
    if args.concurrency != 5:
        argv.extend(["--concurrency", str(args.concurrency)])
    return _run_module_as_main("speedhive.exporters.export_full_dump", argv)


def _report_consistency(args):
    argv = ["--org", str(args.org)]
    if args.db_path != default_db_path():
        argv.extend(["--db-path", str(args.db_path)])
    if args.out_dir != "./output":
        argv.extend(["--out-dir", str(args.out_dir)])
    if args.min_laps != 20:
        argv.extend(["--min-laps", str(args.min_laps)])
    if args.top != 10:
        argv.extend(["--top", str(args.top)])
    if args.threshold != 0.85:
        argv.extend(["--threshold", str(args.threshold)])
    if args.driver:
        argv.extend(["--driver", args.driver])
    if args.ignore_outliers:
        argv.append("--ignore-outliers")
    return _run_module_as_main("speedhive.analyzers.analyze_consistency", argv)


def _extract_driver_laps(args):
    argv = ["--org", str(args.org), "--driver", args.driver]
    if args.driver_keys:
        argv.extend(["--driver-keys", args.driver_keys])
    if args.db_path != default_db_path():
        argv.extend(["--db-path", str(args.db_path)])
    if args.out_dir != Path("./output"):
        argv.extend(["--out-dir", str(args.out_dir)])
    if args.threshold != 0.8:
        argv.extend(["--threshold", str(args.threshold)])
    if args.min_laps != 1:
        argv.extend(["--min-laps", str(args.min_laps)])
    if args.ignore_outliers:
        argv.append("--ignore-outliers")
    return _run_module_as_main("speedhive.analyzers.analyze_driver_laps", argv)


def _extract_track_records(args):
    argv = ["--org", str(args.org)]
    if args.classification:
        argv.extend(["--classification", args.classification])
    if args.db_path != default_db_path():
        argv.extend(["--db-path", str(args.db_path)])
    if args.output:
        argv.extend(["--output", str(args.output)])
    return _run_module_as_main("speedhive.exporters.export_track_records", argv)


def _export_curated_track_records(args):
    body = export_curated_track_records_ndjson(args.org, args.track_records_root)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)
    return 0


def _import_curated_track_records(args):
    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Error: input file does not exist at '{in_path}'.", file=sys.stderr)
        return 1
    try:
        notice = import_curated_track_records_ndjson(
            args.org,
            args.track_records_root,
            in_path.read_text(encoding="utf-8"),
            replace=args.mode == "replace",
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(notice)
    return 0


def _scan_track_records(args):
    from speedhive.storage import SpeedhiveStorage
    from speedhive.workflows.track_records import curation as track_records
    from speedhive.settings import get_bulk_parser_for_org

    storage = SpeedhiveStorage(args.db_path)
    result = track_records.run_sync_and_diff(
        args.org, storage, args.track_records_root, bulk_parser=get_bulk_parser_for_org(args.org)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _refresh_track_records(args):
    from speedhive.storage import SpeedhiveStorage
    from speedhive.workflows.track_records import curation as track_records
    from speedhive.settings import get_bulk_parser_for_org

    client = SpeedhiveClient.create()
    storage = SpeedhiveStorage(args.db_path)
    outcome = track_records.refresh_and_scan(
        args.org,
        client,
        storage,
        args.track_records_root,
        mode=args.mode,
        force=args.force,
        max_events=args.max_events,
        recent_backfill_events=args.recent_backfill_events,
        cleanup_on_full=not args.no_cleanup_on_full,
        bulk_parser=get_bulk_parser_for_org(args.org),
    )
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0


def _build_sync_argv(args) -> list[str]:
    argv = ["--org", str(args.org), "--db-path", str(args.db_path), "--mode", args.mode]
    if args.max_events is not None:
        argv.extend(["--max-events", str(args.max_events)])
    if args.recent_backfill_events:
        argv.extend(["--recent-backfill-events", str(args.recent_backfill_events)])
    if args.token:
        argv.extend(["--token", args.token])
    return argv


def _sync_org(args):
    argv = _build_sync_argv(args)
    return _run_module_as_main("speedhive.workflows.refresh_org_cache", argv)


def _import_dump(args):
    argv = ["--org", str(args.org)]
    if args.db_path != default_db_path():
        argv.extend(["--db-path", str(args.db_path)])
    if args.dump_dir != Path("./output"):
        argv.extend(["--dump-dir", str(args.dump_dir)])
    return _run_module_as_main("speedhive.workflows.import_sqlite_dump", argv)


def _export_lap_records(args):
    argv = ["--org", str(args.org)]
    if args.max_events:
        argv.extend(["--max-events", str(args.max_events)])
    if args.db_path != default_db_path():
        argv.extend(["--db-path", str(args.db_path)])
    if args.output:
        argv.extend(["--output", str(args.output)])
    return _run_module_as_main("speedhive.exporters.export_lap_records", argv)


def _export_db_dump(args):
    argv = ["--org", str(args.org), "--output-dir", str(args.output_dir)]
    if args.db_path != default_db_path():
        argv.extend(["--db-path", str(args.db_path)])
    if args.max_events is not None:
        argv.extend(["--max-events", str(args.max_events)])
    return _run_module_as_main("speedhive.exporters.export_db_dump", argv)


def _configure_org(args):
    from speedhive.llm import DEFAULT_MODEL
    from speedhive.settings import org_settings_path, read_org_settings, write_org_settings

    # If org is not passed via CLI, prompt for it
    org_id = args.org
    if not org_id:
        try:
            val = input("Enter Organization ID: ").strip()
            if not val:
                print("Error: Organization ID is required.")
                return 1
            org_id = int(val)
        except ValueError:
            print("Error: Organization ID must be an integer.")
            return 1

    settings_file = org_settings_path(org_id)
    config = read_org_settings(org_id)

    # Extract existing values
    parsing = config.get("parsing", {})
    parser_default = parsing.get("engine", "regex")

    stats = config.get("stats", {})
    min_laps_default = stats.get("min_laps", 20)

    overrides = config.get("overrides", {})
    anthropic_key_default = overrides.get("ANTHROPIC_API_KEY", os.environ.get(f"ANTHROPIC_API_KEY_{org_id}", os.environ.get("ANTHROPIC_API_KEY", "")))
    anthropic_model_default = overrides.get("ANTHROPIC_MODEL", os.environ.get(f"ANTHROPIC_MODEL_{org_id}", os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)))

    print(f"\n--- Configuring Organization {org_id} ---")
    print(f"Configuration file will be saved to: {settings_file.resolve()}\n")
    print("Note: notification/email settings are configured through the speedhive-tools-ui web Settings page, not here.\n")

    # Helper function for text prompts
    def prompt_text(prompt_text, default_val, is_secret=False):
        display_default = default_val
        if is_secret and default_val:
            display_default = "••••" + default_val[-4:] if len(default_val) > 4 else "••••"
        
        default_prompt = f" [{display_default}]" if default_val else ""
        val = input(f"{prompt_text}{default_prompt}: ").strip()
        if not val:
            return default_val
        return val

    # Helper function for choice prompts
    def prompt_choices(prompt_text, choices, default_val):
        val = input(f"{prompt_text} ({'/'.join(choices)}) [{default_val}]: ").strip().lower()
        if not val:
            return default_val
        if val in choices:
            return val
        print(f"Invalid choice '{val}'. Falling back to default '{default_val}'.")
        return default_val

    # Prompt user
    parser_engine = prompt_choices("Announcer parser engine", ["regex", "llm"], parser_default)

    anthropic_key = ""
    anthropic_model = ""
    if parser_engine == "llm":
        anthropic_key = prompt_text("Anthropic API Key", anthropic_key_default, is_secret=True)
        anthropic_model = prompt_text("Claude Model", anthropic_model_default)
        # Accepting the built-in default stores nothing, so the org keeps
        # following DEFAULT_MODEL instead of being pinned to today's value.
        if anthropic_model == DEFAULT_MODEL:
            anthropic_model = ""

    min_laps = 20
    try:
        min_laps_str = prompt_text("Minimum laps for consistency statistics", str(min_laps_default))
        min_laps = int(min_laps_str)
    except ValueError:
        print("Invalid number for minimum laps. Using default.")
        min_laps = min_laps_default

    # Save to settings.json
    config["parsing"] = {"engine": parser_engine}
    config["stats"] = {"min_laps": min_laps}

    if "overrides" not in config:
        config["overrides"] = {}

    for key, val in [
        ("ANTHROPIC_API_KEY", anthropic_key),
        ("ANTHROPIC_MODEL", anthropic_model),
    ]:
        if val:
            config["overrides"][key] = val
        else:
            config["overrides"].pop(key, None)

    if not config["overrides"]:
        config.pop("overrides", None)

    write_org_settings(org_id, config)

    print(f"\nConfiguration saved successfully to {settings_file.name}!")
    return 0


def main():
    from dotenv import load_dotenv
    load_dotenv()
    # Per-org overrides (ANTHROPIC_API_KEY_<org>, etc.) live in
    # <SPEEDHIVE_DATA_DIR>/orgs/<org_id>/settings.json -- see speedhive.settings.

    parser = argparse.ArgumentParser(description="Speedhive Tools")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("export-dump", help="Export a raw offline NDJSON dump for an organization")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--output", default="./output", help="Root output directory for NDJSON dump files")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=5)
    p.set_defaults(func=_export_full_dump)

    p = sub.add_parser("report-consistency", help="Report top/bottom consistency from the primary SQLite cache")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--out-dir", default="./output", help="Output directory for generated reports")
    p.add_argument("--min-laps", type=int, default=20)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--threshold", type=float, default=0.85)
    p.add_argument("--driver", default=None)
    p.add_argument("--ignore-outliers", action="store_true", help="Ignore outlier lap times using IQR method")
    p.set_defaults(func=_report_consistency)

    p = sub.add_parser("extract-driver-laps", help="Extract laps for a driver from the primary SQLite cache")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--driver", required=True, help="Driver name to fuzzy match")
    p.add_argument("--driver-keys", default=None)
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--out-dir", type=Path, default=Path("./output"), help="Output directory for generated reports")
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument("--min-laps", type=int, default=1)
    p.add_argument("--ignore-outliers", action="store_true", help="Ignore outlier lap times using IQR method")
    p.set_defaults(func=_extract_driver_laps)

    p = sub.add_parser("export-track-records", help="Export track records from the primary SQLite cache to NDJSON")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--classification", default=None)
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--output", default=None, help="Output file path (NDJSON)")
    p.set_defaults(func=_extract_track_records)

    p = sub.add_parser("export-curated-track-records", help="Export curated track records from the workflow store to NDJSON")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--track-records-root", type=Path, default=Path("./data/orgs"), help="Track-record workflow storage root")
    p.add_argument("--output", default=None, help="Output file path (NDJSON)")
    p.set_defaults(func=_export_curated_track_records)

    p = sub.add_parser("import-curated-track-records", help="Import curated track records into the workflow store from NDJSON")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--track-records-root", type=Path, default=Path("./data/orgs"), help="Track-record workflow storage root")
    p.add_argument("--input", required=True, help="Input file path (NDJSON)")
    p.add_argument("--mode", choices=["merge", "replace"], default="merge")
    p.set_defaults(func=_import_curated_track_records)

    p = sub.add_parser("scan-track-records", help="Diff track records against the curated store without refreshing the org cache")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--track-records-root", type=Path, default=Path("./data/orgs"), help="Track-records storage root")
    p.set_defaults(func=_scan_track_records)

    p = sub.add_parser("refresh-track-records", help="Refresh the org cache if needed, then scan track records")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--track-records-root", type=Path, default=Path("./data/orgs"), help="Track-records storage root")
    p.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    p.add_argument("--force", action="store_true", help="Refresh and scan even if the cache appears fresh")
    p.add_argument("--max-events", type=int, default=None, help="Maximum number of events to refresh")
    p.add_argument("--recent-backfill-events", type=int, default=20, help="Number of recent events to backfill in incremental mode")
    p.add_argument("--no-cleanup-on-full", action="store_true", help="Skip pruning removed events after a full refresh")
    p.set_defaults(func=_refresh_track_records)

    p = sub.add_parser("sync-org", help="Sync org data into the primary SQLite cache")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--recent-backfill-events", type=int, default=0)
    p.add_argument("--token", default=None)
    p.set_defaults(func=_sync_org)

    p = sub.add_parser("import-dump", help="Import an offline NDJSON dump into the primary SQLite cache")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--dump-dir", type=Path, default=Path("./output"), help="Root directory containing exported NDJSON dump files")
    p.set_defaults(func=_import_dump)

    p = sub.add_parser("export-lap-records", help="Export lap records from the primary SQLite cache to NDJSON")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--max-events", type=int, default=25, help="Maximum number of events to export")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--output", "-o", default=None, help="Output NDJSON file path (default: stdout)")
    p.set_defaults(func=_export_lap_records)

    p = sub.add_parser("export-db-dump", help="Export an offline NDJSON dump of an organization from the primary SQLite cache")
    p.add_argument("--org", type=int, required=True, help="Organization ID")
    p.add_argument("--output-dir", type=Path, required=True, help="Directory to save NDJSON files")
    p.add_argument("--db-path", type=Path, default=default_db_path(), help="Primary SQLite cache path")
    p.add_argument("--max-events", type=int, default=None, help="Maximum number of events to export")
    p.set_defaults(func=_export_db_dump)

    p = sub.add_parser("configure", help="Run an interactive setup wizard to configure organization settings")
    p.add_argument("--org", type=int, default=None, help="Organization ID (optional, will prompt if omitted)")
    p.set_defaults(func=_configure_org)

    register_discovered(sub)

    args = parser.parse_args()
    if hasattr(args, "func"):
        return args.func(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
