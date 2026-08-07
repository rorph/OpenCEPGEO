from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .database import build_database, lookup
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
    source_metadata = build.add_mutually_exclusive_group(required=True)
    source_metadata.add_argument("--source-lock", help="checksum-locked input manifest")
    source_metadata.add_argument(
        "--source-version", help="immutable fixture input version"
    )
    build.add_argument("--output", required=True, help="output SQLite path")
    build.add_argument("--export", help="canonical CEP-sorted JSONL output")
    build.add_argument("--manifest", help="deterministic build manifest")
    build.add_argument("--min-prefix-samples", type=int, default=3)
    build.add_argument("--max-prefix-radius-km", type=float, default=25.0)
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

    if args.command == "build":
        output = Path(args.output)
        stats = build_database(
            opencep_path=args.opencep,
            ibge_path=args.ibge,
            observations_path=args.observations,
            source_version=args.source_version,
            source_lock_path=args.source_lock,
            output_path=output,
            export_path=args.export or output.with_suffix(".jsonl"),
            manifest_path=args.manifest or output.with_suffix(".manifest.json"),
            min_prefix_samples=args.min_prefix_samples,
            max_prefix_radius_km=args.max_prefix_radius_km,
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
