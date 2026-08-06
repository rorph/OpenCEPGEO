from __future__ import annotations

import argparse
import json
import sys

from .database import build_database, lookup


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opencepgeo",
        description="Build and query an offline Brazilian CEP centroid database.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build a versioned SQLite artifact")
    build.add_argument("--opencep", required=True, help="OpenCEP release ZIP or directory")
    build.add_argument("--ibge", required=True, help="IBGE Localidades GeoPackage")
    build.add_argument("--observations", help="optional trusted CEP observations CSV")
    build.add_argument("--source-version", required=True, help="immutable input version label")
    build.add_argument("--output", required=True, help="output SQLite path")
    build.add_argument("--min-prefix-samples", type=int, default=3)
    build.add_argument("--max-prefix-radius-km", type=float, default=25.0)
    build.add_argument("--force", action="store_true")

    query = commands.add_parser("lookup", help="look up one CEP in a local artifact")
    query.add_argument("--database", required=True)
    query.add_argument("cep")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "build":
        stats = build_database(
            opencep_path=args.opencep,
            ibge_path=args.ibge,
            observations_path=args.observations,
            source_version=args.source_version,
            output_path=args.output,
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

