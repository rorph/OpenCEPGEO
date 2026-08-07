from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .database import build_database, lookup
from .osm import PBFError, extract_postcode_nodes
from .quality import (
    build_quality_report,
    quality_report_markdown,
    write_quality_report,
)
from .release import package_release, verify_release
from .source_lock import SourceLockError, fetch_sources, verify_sources


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencepgeo",
        description="Build and query an offline Brazilian CEP centroid database.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build a versioned SQLite artifact")
    build.add_argument(
        "--opencep", required=True, help="OpenCEP release ZIP or directory"
    )
    build.add_argument("--ibge", required=True, help="IBGE Localidades GeoPackage")
    build.add_argument("--observations", help="optional trusted CEP observations CSV")
    build.add_argument("--osm-observations", help="local extracted OSM postcode CSV")
    build.add_argument(
        "--municipality-boundaries",
        help="official IBGE municipality polygon ZIP used to validate OSM evidence",
    )
    build.add_argument(
        "--config",
        default="config/enrichment-v1.json",
        help="versioned enrichment thresholds",
    )
    build.add_argument(
        "--quality-config",
        default="config/quality-v1.json",
        help="versioned build regression thresholds",
    )
    source_metadata = build.add_mutually_exclusive_group(required=True)
    source_metadata.add_argument("--source-lock", help="checksum-locked input manifest")
    source_metadata.add_argument(
        "--source-version", help="immutable fixture input version"
    )
    build.add_argument("--output", required=True, help="output SQLite path")
    build.add_argument("--export", help="canonical CEP-sorted JSONL output")
    build.add_argument("--manifest", help="deterministic build manifest")
    build.add_argument("--force", action="store_true")

    query = commands.add_parser("lookup", help="look up one CEP in a local artifact")
    query.add_argument("--database", required=True)
    query.add_argument("cep")

    sources = commands.add_parser(
        "sources", help="fetch or verify checksum-locked inputs"
    )
    source_commands = sources.add_subparsers(dest="source_command", required=True)
    for command in ("fetch", "verify"):
        source = source_commands.add_parser(command)
        source.add_argument("--lock", default="sources/lock.json")
        source.add_argument("--input-dir", required=True)
        source.add_argument("--source", action="append", dest="source_ids")
        source.add_argument("--include-optional", action="store_true")
        if command == "fetch":
            source.add_argument("--timeout", type=float, default=60.0)

    osm = commands.add_parser("osm", help="extract local OSM postcode evidence")
    osm_commands = osm.add_subparsers(dest="osm_command", required=True)
    osm_extract = osm_commands.add_parser("extract")
    osm_extract.add_argument("--pbf", required=True)
    osm_extract.add_argument("--source-lock", default="sources/lock.json")
    osm_extract.add_argument("--output", required=True)
    osm_extract.add_argument("--manifest")
    osm_extract.add_argument("--force", action="store_true")

    quality = commands.add_parser("quality", help="run validation quality gates")
    quality_commands = quality.add_subparsers(dest="quality_command", required=True)
    quality_report = quality_commands.add_parser("report")
    quality_report.add_argument("--database", required=True)
    quality_report.add_argument("--build-manifest", required=True)
    quality_report.add_argument("--ibge", required=True)
    quality_report.add_argument("--osm-observations", required=True)
    quality_report.add_argument("--official-holdout", required=True)
    quality_report.add_argument("--official-holdout-id", required=True)
    quality_report.add_argument("--municipality-boundaries", required=True)
    quality_report.add_argument("--config", default="config/enrichment-v1.json")
    quality_report.add_argument("--quality-config", default="config/quality-v1.json")
    quality_report.add_argument("--output", required=True)
    quality_report.add_argument("--markdown")

    release = commands.add_parser("release", help="package or verify a local release")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_package = release_commands.add_parser("package")
    release_package.add_argument("--database", required=True)
    release_package.add_argument("--normalized", required=True)
    release_package.add_argument("--build-manifest", required=True)
    release_package.add_argument("--quality-report", required=True)
    release_package.add_argument("--quality-markdown", required=True)
    release_package.add_argument("--notice", default="NOTICE.md")
    release_package.add_argument("--source-lock", default="sources/lock.json")
    release_package.add_argument(
        "--enrichment-config", default="config/enrichment-v1.json"
    )
    release_package.add_argument("--quality-policy", default="config/quality-v1.json")
    release_package.add_argument("--ibge", required=True)
    release_package.add_argument("--osm-observations", required=True)
    release_package.add_argument("--official-holdout", required=True)
    release_package.add_argument("--official-holdout-id", required=True)
    release_package.add_argument("--municipality-boundaries", required=True)
    release_package.add_argument(
        "--corrections", default="sources/opencep-2.0.1-corrections.json"
    )
    release_package.add_argument("--output", required=True)
    release_verify = release_commands.add_parser("verify")
    release_verify.add_argument("directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "sources":
        try:
            if args.source_command == "fetch":
                results = fetch_sources(
                    args.lock,
                    args.input_dir,
                    source_ids=args.source_ids,
                    include_optional=args.include_optional,
                    timeout=args.timeout,
                )
            else:
                results = verify_sources(
                    args.lock,
                    args.input_dir,
                    source_ids=args.source_ids,
                    include_optional=args.include_optional,
                )
        except SourceLockError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps({"sources": results}, sort_keys=True))
        return 0

    if args.command == "osm":
        try:
            result = extract_postcode_nodes(
                args.pbf,
                args.output,
                source_lock_path=args.source_lock,
                manifest_path=args.manifest,
                force=args.force,
            )
        except (PBFError, SourceLockError, OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "quality":
        try:
            report = build_quality_report(
                database_path=args.database,
                build_manifest_path=args.build_manifest,
                ibge_path=args.ibge,
                osm_observations_path=args.osm_observations,
                official_holdout_path=args.official_holdout,
                official_holdout_source_id=args.official_holdout_id,
                municipality_boundaries_path=args.municipality_boundaries,
                enrichment_config_path=args.config,
                quality_policy_path=args.quality_config,
            )
            write_quality_report(report, args.output)
            if args.markdown:
                Path(args.markdown).write_text(
                    quality_report_markdown(report), encoding="utf-8"
                )
        except (OSError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        print(
            json.dumps(
                {"status": report["status"], "failures": report["failures"]},
                sort_keys=True,
            )
        )
        return 0 if report["status"] == "pass" else 2

    if args.command == "release":
        try:
            if args.release_command == "package":
                result = package_release(
                    database_path=args.database,
                    normalized_path=args.normalized,
                    build_manifest_path=args.build_manifest,
                    quality_report_path=args.quality_report,
                    quality_markdown_path=args.quality_markdown,
                    notice_path=args.notice,
                    source_lock_path=args.source_lock,
                    enrichment_config_path=args.enrichment_config,
                    quality_policy_path=args.quality_policy,
                    ibge_path=args.ibge,
                    osm_observations_path=args.osm_observations,
                    official_holdout_path=args.official_holdout,
                    official_holdout_source_id=args.official_holdout_id,
                    municipality_boundaries_path=args.municipality_boundaries,
                    corrections_path=args.corrections,
                    output_directory=args.output,
                )
            else:
                result = verify_release(args.directory)
        except (FileExistsError, OSError, sqlite3.DatabaseError, ValueError) as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps(result, sort_keys=True))
        return 0

    if args.command == "build":
        output = Path(args.output)
        stats = build_database(
            opencep_path=args.opencep,
            ibge_path=args.ibge,
            observations_path=args.observations,
            osm_observations_path=args.osm_observations,
            municipality_boundaries_path=args.municipality_boundaries,
            enrichment_config_path=args.config,
            quality_config_path=args.quality_config,
            source_version=args.source_version,
            source_lock_path=args.source_lock,
            output_path=output,
            export_path=args.export or output.with_suffix(".jsonl"),
            manifest_path=args.manifest or output.with_suffix(".manifest.json"),
            force=args.force,
        )
        print(json.dumps(stats, sort_keys=True))
        return 0

    result = lookup(args.database, args.cep)
    if result is None:
        print(json.dumps({"error": "CEP not found"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
