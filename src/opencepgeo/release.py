from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .database import _contract_row
from .quality import (
    build_quality_report,
    load_quality_policy,
    quality_report_markdown,
    validate_quality_report,
)
from .provenance import builder_identity

_RELEASE_FORMAT = "opencepgeo-release-manifest-v2"
_BUILD_MANIFEST_FORMAT = "opencepgeo-build-manifest-v2"
_QUALITY_REPORT_FORMAT = "opencepgeo-quality-report-v2"
_QUALITY_POLICY_FORMAT = "opencepgeo-quality-policy-v2"
_SCHEMA_VERSION = "opencepgeo-sqlite-v4"
_CSV_FORMAT = "opencepgeo-csv-v4"
_JSONL_FORMAT = "opencepgeo-jsonl-v4"
_MAX_CSV_FIELD_BYTES = 1024 * 1024
_MAX_PROVENANCE_BYTES = 2048
_CSV_COLUMNS = (
    "cep",
    "prefix",
    "street",
    "complement",
    "unit",
    "neighborhood",
    "city",
    "uf",
    "state",
    "region",
    "ibge",
    "latitude",
    "longitude",
    "precision",
    "method",
    "evidence_count",
    "evidence_radius_km",
    "geo_source",
    "evidence_digest",
    "dataset_version",
)
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


def _artifact(path: Path, file_format: str) -> dict[str, object]:
    byte_size, digest = _sha256(path)
    return {"bytes": byte_size, "sha256": digest, "format": file_format}


def _load_json(
    path: str | Path, expected_format: str | None = None
) -> dict[str, object]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON document {source}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"JSON document must be an object: {source}")
    if expected_format is not None and document.get("format") != expected_format:
        raise ValueError(f"incompatible format in {source}: {document.get('format')!r}")
    return document


def _open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        return dict(connection.execute("SELECT key, value FROM metadata"))
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"incompatible SQLite artifact: {exc}") from exc


def _validate_database(
    path: Path,
    *,
    expected_version: str | None = None,
    expected_records: int | None = None,
) -> tuple[str, int, dict[str, str]]:
    connection = _open_database(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")
        metadata = _metadata(connection)
        if metadata.get("format") != _SCHEMA_VERSION:
            raise ValueError(f"incompatible SQLite schema: {metadata.get('format')!r}")
        version = metadata.get("dataset_version")
        if not version or not _SAFE_VERSION.fullmatch(version):
            raise ValueError(f"invalid dataset version: {version!r}")
        if expected_version is not None and version != expected_version:
            raise ValueError(
                f"dataset version mismatch: {version!r} != {expected_version!r}"
            )
        records = connection.execute("SELECT count(*) FROM cep_geo").fetchone()[0]
        if expected_records is not None and records != expected_records:
            raise ValueError(
                f"SQLite row-count regression: {records} != {expected_records}"
            )
        invalid_geo = connection.execute(
            """
            SELECT count(*) FROM cep_geo
             WHERE (latitude IS NULL) != (longitude IS NULL)
                OR (latitude IS NULL AND (
                    precision IS NOT NULL OR method IS NOT NULL OR
                    evidence_count IS NOT NULL OR evidence_radius_km IS NOT NULL OR
                    geo_source IS NOT NULL OR evidence_digest IS NOT NULL
                ))
                OR (latitude IS NOT NULL AND (
                    precision IS NULL OR method IS NULL OR evidence_count IS NULL OR
                    evidence_count <= 0 OR evidence_radius_km IS NULL OR
                    evidence_radius_km < 0 OR geo_source IS NULL OR
                    evidence_digest IS NULL OR latitude < -90 OR latitude > 90 OR
                    longitude < -180 OR longitude > 180
                ))
                OR length(cep) != 8 OR cep GLOB '*[^0-9]*'
                OR length(prefix) != 5 OR prefix GLOB '*[^0-9]*'
                OR prefix != substr(cep, 1, 5)
                OR length(ibge) != 7 OR ibge GLOB '*[^0-9]*'
                OR length(uf) != 2
                OR dataset_version != ?
            """,
            (version,),
        ).fetchone()[0]
        if invalid_geo:
            raise ValueError(f"invalid additive geo contract rows: {invalid_geo}")
        maximum_provenance, invalid_digests = connection.execute(
            """
            SELECT coalesce(max(length(geo_source)), 0),
                   sum(CASE WHEN latitude IS NOT NULL AND (
                       evidence_digest IS NULL OR length(evidence_digest) != 71 OR
                       substr(evidence_digest, 1, 7) != 'sha256:' OR
                       substr(evidence_digest, 8) GLOB '*[^0-9a-f]*'
                   ) THEN 1 ELSE 0 END)
              FROM cep_geo
            """
        ).fetchone()
        if maximum_provenance > _MAX_PROVENANCE_BYTES:
            raise ValueError(
                f"geo provenance exceeds CSV field contract: {maximum_provenance} bytes"
            )
        if invalid_digests:
            raise ValueError(f"invalid evidence digest rows: {invalid_digests}")
        for (raw_sources,) in connection.execute(
            "SELECT DISTINCT geo_source FROM cep_geo WHERE geo_source IS NOT NULL"
        ):
            try:
                sources = json.loads(raw_sources)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid geo_source JSON") from exc
            if (
                not isinstance(sources, list)
                or not 1 <= len(sources) <= 16
                or any(not isinstance(source, str) or not source for source in sources)
                or sources != sorted(set(sources))
            ):
                raise ValueError("invalid geo_source category contract")
        builder = {
            "name": metadata.get("builder_name"),
            "version": metadata.get("builder_version"),
            "source_tree_sha256": metadata.get("builder_source_tree_sha256"),
        }
        if (
            not all(isinstance(value, str) and value for value in builder.values())
            or not re.fullmatch(r"[0-9a-f]{64}", str(builder["source_tree_sha256"]))
        ):
            raise ValueError("SQLite builder identity is missing or invalid")
        return version, records, metadata
    finally:
        connection.close()


def _write_csv(database: Path, output: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0

    class DigestWriter:
        def write(self, value: str) -> int:
            nonlocal byte_size
            payload = value.encode("utf-8")
            handle.write(payload)
            digest.update(payload)
            byte_size += len(payload)
            return len(value)

    connection = _open_database(database)
    try:
        with output.open("xb") as handle:
            writer = csv.writer(DigestWriter(), lineterminator="\n")
            writer.writerow(_CSV_COLUMNS)
            query = "SELECT " + ", ".join(_CSV_COLUMNS) + " FROM cep_geo ORDER BY cep"
            for row in connection.execute(query):
                writer.writerow(tuple(row[column] for column in _CSV_COLUMNS))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        connection.close()
    return byte_size, digest.hexdigest()


def _copy(source: str | Path, target: Path) -> None:
    source_path = Path(source)
    if not source_path.is_file():
        raise ValueError(f"release input is not a file: {source_path}")
    shutil.copyfile(source_path, target)


def _attribution_tokens(build_manifest: Mapping[str, object]) -> list[str]:
    tokens: set[str] = set()
    for source in build_manifest.get("sources", []):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("id", "")).lower()
        if "opencep" in source_id:
            tokens.add("OpenCEP")
        if "ibge" in source_id:
            tokens.add("IBGE")
    configuration = build_manifest.get("configuration")
    if isinstance(configuration, dict) and configuration.get("osm_observations"):
        tokens.add("OpenStreetMap")
    inputs = build_manifest.get("inputs")
    if isinstance(inputs, dict) and isinstance(inputs.get("normalized_refresh"), dict):
        tokens.update(("Correios", "OpenCEP"))
    return sorted(tokens)


def _validate_release_inputs(
    *,
    database_path: Path,
    normalized_path: Path,
    build_manifest_path: Path,
    quality: Mapping[str, object],
    quality_policy_path: Path,
    notice_path: Path,
    source_lock_path: Path,
) -> tuple[str, int, dict[str, object], dict[str, object]]:
    build_manifest = _load_json(build_manifest_path, _BUILD_MANIFEST_FORMAT)
    policy = load_quality_policy(quality_policy_path)
    version, records, metadata = _validate_database(database_path)
    if build_manifest.get("dataset_version") != version:
        raise ValueError("build manifest/database version mismatch")
    if build_manifest.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("build manifest has incompatible schema")
    statistics = build_manifest.get("statistics")
    if not isinstance(statistics, dict) or statistics.get("unique_ceps") != records:
        raise ValueError("build manifest row count does not match SQLite")
    artifacts = build_manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("build manifest has no artifact records")
    database_record = artifacts.get("sqlite")
    normalized_record = artifacts.get("normalized")
    if not isinstance(database_record, dict) or not isinstance(normalized_record, dict):
        raise ValueError("build manifest is missing SQLite or normalized artifact")
    if _sha256(database_path)[1] != database_record.get("sha256"):
        raise ValueError("SQLite checksum does not match build manifest")
    if _sha256(normalized_path)[1] != normalized_record.get("sha256"):
        raise ValueError("normalized checksum does not match build manifest")
    validate_quality_report(
        quality,
        policy,
        expected_dataset_version=version,
        expected_inputs={
            "database_sha256": database_record.get("sha256"),
            "build_manifest_sha256": _sha256(build_manifest_path)[1],
            "quality_policy_sha256": policy.sha256,
        },
    )
    if quality.get("status") != "pass":
        raise ValueError("quality report is not passing")
    lock = _load_json(source_lock_path, "opencepgeo-source-lock-v1")
    lock_record = build_manifest.get("source_lock")
    if not isinstance(lock_record, dict) or _sha256(source_lock_path)[
        1
    ] != lock_record.get("sha256"):
        raise ValueError("source lock does not match build manifest")
    if metadata.get("source_lock_sha256") != lock_record.get("sha256"):
        raise ValueError("SQLite source lock metadata does not match package input")
    builder = build_manifest.get("builder")
    if not isinstance(builder, dict) or any(
        metadata.get(metadata_key) != builder.get(builder_key)
        for metadata_key, builder_key in (
            ("builder_name", "name"),
            ("builder_version", "version"),
            ("builder_source_tree_sha256", "source_tree_sha256"),
        )
    ):
        raise ValueError("build manifest/SQLite builder identity mismatch")
    if builder != builder_identity():
        raise ValueError("build artifact was not produced by the current builder")
    notice = notice_path.read_text(encoding="utf-8")
    missing = [
        token for token in _attribution_tokens(build_manifest) if token not in notice
    ]
    if missing:
        raise ValueError(f"source notice is missing attribution: {', '.join(missing)}")
    publication_gate = lock.get("publication_gate")
    if not isinstance(publication_gate, str) or not publication_gate:
        raise ValueError("source lock publication gate is missing")
    if publication_gate != lock_record.get("publication_gate"):
        raise ValueError("source lock publication gate does not match build manifest")
    return version, records, build_manifest, dict(quality)


def package_release(
    *,
    database_path: str | Path,
    normalized_path: str | Path,
    build_manifest_path: str | Path,
    quality_report_path: str | Path,
    quality_markdown_path: str | Path,
    notice_path: str | Path,
    source_lock_path: str | Path,
    enrichment_config_path: str | Path,
    quality_policy_path: str | Path,
    ibge_path: str | Path,
    osm_observations_path: str | Path,
    official_holdout_path: str | Path,
    official_holdout_source_id: str,
    municipality_boundaries_path: str | Path,
    corrections_path: str | Path | None,
    output_directory: str | Path,
) -> dict[str, object]:
    database = Path(database_path)
    normalized = Path(normalized_path)
    build_manifest_source = Path(build_manifest_path)
    quality_source = Path(quality_report_path)
    quality_markdown_source = Path(quality_markdown_path)
    notice = Path(notice_path)
    source_lock = Path(source_lock_path)
    quality = _load_json(quality_source, _QUALITY_REPORT_FORMAT)
    recomputed = build_quality_report(
        database_path=database,
        build_manifest_path=build_manifest_source,
        ibge_path=ibge_path,
        osm_observations_path=osm_observations_path,
        official_holdout_path=official_holdout_path,
        official_holdout_source_id=official_holdout_source_id,
        municipality_boundaries_path=municipality_boundaries_path,
        enrichment_config_path=enrichment_config_path,
        quality_policy_path=quality_policy_path,
    )
    canonical_quality = (
        json.dumps(
            recomputed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
    if quality != recomputed or quality_source.read_text(
        encoding="utf-8"
    ) != canonical_quality:
        raise ValueError("quality report is not the canonical recomputed report")
    expected_markdown = quality_report_markdown(recomputed)
    if quality_markdown_source.read_text(encoding="utf-8") != expected_markdown:
        raise ValueError("quality Markdown does not match the recomputed report")
    version, records, build_manifest, _quality = _validate_release_inputs(
        database_path=database,
        normalized_path=normalized,
        build_manifest_path=build_manifest_source,
        quality=quality,
        quality_policy_path=Path(quality_policy_path),
        notice_path=notice,
        source_lock_path=source_lock,
    )
    configuration = build_manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("build manifest has no configuration records")
    expected_inputs = [
        (enrichment_config_path, configuration.get("enrichment"), "enrichment"),
        (quality_policy_path, configuration.get("quality"), "quality policy"),
    ]
    corrections_record = configuration.get("opencep_corrections")
    if corrections_record is not None:
        if corrections_path is None:
            raise ValueError("build requires a packaged corrections file")
        expected_inputs.append((corrections_path, corrections_record, "corrections"))
    for path, record, label in expected_inputs:
        if not isinstance(record, dict) or _sha256(Path(path))[1] != record.get(
            "sha256"
        ):
            raise ValueError(f"{label} input does not match build manifest")
    output = Path(output_directory)
    if output.exists():
        raise FileExistsError(f"release output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    )
    try:
        filenames = {
            "sqlite": f"opencepgeo-{version}.sqlite",
            "jsonl": f"opencepgeo-{version}.jsonl",
            "csv": f"opencepgeo-{version}.csv",
            "build_manifest": "build-manifest.json",
            "quality_report": "quality-report.json",
            "quality_summary": "quality-report.md",
            "notice": "NOTICE.md",
            "source_lock": "source-lock.json",
            "enrichment_config": "enrichment-config.json",
            "quality_policy": "quality-policy.json",
        }
        if corrections_record is not None:
            filenames["corrections"] = "opencep-corrections.json"
        _copy(database, temporary / filenames["sqlite"])
        _copy(normalized, temporary / filenames["jsonl"])
        _copy(build_manifest_source, temporary / filenames["build_manifest"])
        _copy(quality_source, temporary / filenames["quality_report"])
        _copy(quality_markdown_source, temporary / filenames["quality_summary"])
        _copy(notice, temporary / filenames["notice"])
        _copy(source_lock, temporary / filenames["source_lock"])
        _copy(enrichment_config_path, temporary / filenames["enrichment_config"])
        _copy(quality_policy_path, temporary / filenames["quality_policy"])
        if corrections_record is not None:
            assert corrections_path is not None
            _copy(corrections_path, temporary / filenames["corrections"])
        csv_bytes, csv_sha256 = _write_csv(database, temporary / filenames["csv"])

        file_formats = {
            filenames["sqlite"]: _SCHEMA_VERSION,
            filenames["jsonl"]: _JSONL_FORMAT,
            filenames["csv"]: _CSV_FORMAT,
            filenames["build_manifest"]: _BUILD_MANIFEST_FORMAT,
            filenames["quality_report"]: _QUALITY_REPORT_FORMAT,
            filenames["quality_summary"]: "markdown",
            filenames["notice"]: "markdown",
            filenames["source_lock"]: "opencepgeo-source-lock-v1",
            filenames["enrichment_config"]: "opencepgeo-enrichment-v1",
            filenames["quality_policy"]: _QUALITY_POLICY_FORMAT,
        }
        if corrections_record is not None:
            file_formats[filenames["corrections"]] = "opencepgeo-corrections-v1"
        files = {
            name: _artifact(temporary / name, file_formats[name])
            for name in sorted(file_formats)
        }
        if files[filenames["csv"]] != {
            "bytes": csv_bytes,
            "sha256": csv_sha256,
            "format": _CSV_FORMAT,
        }:
            raise AssertionError("CSV streaming checksum mismatch")
        source_lock_record = build_manifest["source_lock"]
        assert isinstance(source_lock_record, dict)
        quality_inputs = quality["inputs"]
        assert isinstance(quality_inputs, dict)
        release_manifest = {
            "format": _RELEASE_FORMAT,
            "dataset_version": version,
            "schema_version": _SCHEMA_VERSION,
            "record_count": records,
            "release_status": "blocked-private-release-candidate",
            "publication_gate": source_lock_record["publication_gate"],
            "source_lock_sha256": source_lock_record["sha256"],
            "builder": build_manifest["builder"],
            "quality_attestation": {
                "mode": "package-time-recomputed-from-manifest-bound-evidence-v1",
                "distribution_verification": "attestation-only-evidence-not-packaged",
                "report_format": _QUALITY_REPORT_FORMAT,
                "report_sha256": files[filenames["quality_report"]]["sha256"],
                "database_sha256": quality_inputs["database_sha256"],
                "build_manifest_sha256": quality_inputs["build_manifest_sha256"],
                "ibge_sha256": quality_inputs["ibge_sha256"],
                "osm_observations_sha256": quality_inputs[
                    "osm_observations_sha256"
                ],
                "official_holdout_sha256": quality_inputs[
                    "official_holdout_sha256"
                ],
                "official_holdout_source_id": quality_inputs[
                    "official_holdout_source_id"
                ],
                "official_holdout_filename": quality_inputs[
                    "official_holdout_filename"
                ],
                "official_holdout_bytes": quality_inputs[
                    "official_holdout_bytes"
                ],
                "official_holdout_path_contract": quality_inputs[
                    "official_holdout_path_contract"
                ],
                "municipality_boundaries_sha256": quality_inputs[
                    "municipality_boundaries_sha256"
                ],
                "enrichment_config_sha256": quality_inputs[
                    "enrichment_config_sha256"
                ],
                "quality_policy_sha256": quality_inputs[
                    "quality_policy_sha256"
                ],
                "cohort_split": {
                    "algorithm": quality["cohorts"]["unseen_cep"]["algorithm"],
                    "modulus": quality["cohorts"]["unseen_cep"]["modulus"],
                    "remainder": quality["cohorts"]["unseen_cep"]["remainder"],
                },
            },
            "required_attribution": _attribution_tokens(build_manifest),
            "lookup_contract": {
                "offline": True,
                "geojson_coordinate_order": "longitude,latitude",
                "unknown_cep": None,
                "unresolved_geo": None,
                "precision_required_when_located": True,
                "evidence_radius_is_calibrated_error": False,
            },
            "files": files,
        }
        manifest_payload = (
            json.dumps(
                release_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        (temporary / "manifest.json").write_text(manifest_payload, encoding="utf-8")
        checksummed = sorted((*files, "manifest.json"))
        sums = "".join(
            f"{_sha256(temporary / name)[1]}  {name}\n" for name in checksummed
        )
        (temporary / "SHA256SUMS").write_text(sums, encoding="utf-8")
        os.replace(temporary, output)
        return {
            "dataset_version": version,
            "record_count": records,
            "output": str(output),
            "release_status": release_manifest["release_status"],
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parse_sums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        parts = line.split("  ", 1)
        if (
            len(parts) != 2
            or not re.fullmatch(r"[0-9a-f]{64}", parts[0])
            or not parts[1]
        ):
            raise ValueError(f"invalid SHA256SUMS line {line_number}")
        digest, name = parts
        if name in entries or Path(name).name != name:
            raise ValueError(f"invalid or duplicate SHA256SUMS filename: {name}")
        entries[name] = digest
    return entries


def _verify_sorted_export(
    path: Path,
    *,
    expected_records: int,
    file_format: str,
    database_path: Path | None = None,
) -> None:
    previous = ""
    count = 0
    database = _open_database(database_path) if database_path is not None else None
    database_rows = (
        iter(database.execute("SELECT * FROM cep_geo ORDER BY cep"))
        if database is not None
        else None
    )
    try:
        if file_format == _CSV_FORMAT:
            previous_limit = csv.field_size_limit()
            csv.field_size_limit(max(previous_limit, _MAX_CSV_FIELD_BYTES))
            try:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    if tuple(reader.fieldnames or ()) != _CSV_COLUMNS:
                        raise ValueError("CSV has an incompatible header")
                    for row in reader:
                        cep = row.get("cep")
                        if (
                            cep is None
                            or len(cep) != 8
                            or not cep.isdigit()
                            or cep <= previous
                        ):
                            raise ValueError(
                                "CSV CEPs are invalid, duplicate, or unsorted"
                            )
                        if database_rows is not None:
                            expected_row = next(database_rows, None)
                            expected = (
                                {
                                    column: ""
                                    if expected_row[column] is None
                                    else str(expected_row[column])
                                    for column in _CSV_COLUMNS
                                }
                                if expected_row is not None
                                else None
                            )
                            if row != expected:
                                raise ValueError(
                                    f"CSV row does not match SQLite for CEP {cep}"
                                )
                        previous = cep
                        count += 1
            except csv.Error as exc:
                raise ValueError(f"invalid CSV export: {exc}") from exc
            finally:
                csv.field_size_limit(previous_limit)
        else:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"invalid JSONL row {line_number}: {exc}"
                        ) from exc
                    cep = row.get("cep") if isinstance(row, dict) else None
                    if (
                        not isinstance(cep, str)
                        or len(cep) != 8
                        or not cep.isdigit()
                        or cep <= previous
                    ):
                        raise ValueError(
                            "JSONL CEPs are invalid, duplicate, or unsorted"
                        )
                    if database_rows is not None:
                        expected_row = next(database_rows, None)
                        expected = (
                            _contract_row(expected_row)
                            if expected_row is not None
                            else None
                        )
                        if row != expected:
                            raise ValueError(
                                f"JSONL row does not match SQLite for CEP {cep}"
                            )
                    previous = cep
                    count += 1
        if database_rows is not None and next(database_rows, None) is not None:
            raise ValueError(f"{file_format} is missing SQLite rows")
        if count != expected_records:
            raise ValueError(
                f"{file_format} row-count regression: {count} != {expected_records}"
            )
    finally:
        if database is not None:
            database.close()


def verify_release(directory: str | Path) -> dict[str, object]:
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"release directory does not exist: {root}")
    manifest = _load_json(root / "manifest.json", _RELEASE_FORMAT)
    expected_manifest_fields = {
        "format",
        "dataset_version",
        "schema_version",
        "record_count",
        "release_status",
        "publication_gate",
        "source_lock_sha256",
        "builder",
        "quality_attestation",
        "required_attribution",
        "lookup_contract",
        "files",
    }
    if set(manifest) != expected_manifest_fields:
        raise ValueError("release manifest has incomplete or unexpected fields")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("release manifest has incompatible schema")
    if manifest.get("lookup_contract") != {
        "offline": True,
        "geojson_coordinate_order": "longitude,latitude",
        "unknown_cep": None,
        "unresolved_geo": None,
        "precision_required_when_located": True,
        "evidence_radius_is_calibrated_error": False,
    }:
        raise ValueError("release manifest has incompatible lookup contract")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("release manifest has no files")
    sums = _parse_sums(root / "SHA256SUMS")
    expected_names = set(files) | {"manifest.json", "SHA256SUMS"}
    entries = list(root.iterdir())
    non_files = sorted(
        path.name for path in entries if not path.is_file() or path.is_symlink()
    )
    if non_files:
        raise ValueError(f"release contains non-file entries: {non_files}")
    actual_names = {path.name for path in entries}
    if actual_names != expected_names:
        raise ValueError(
            f"release file set mismatch: missing={sorted(expected_names - actual_names)}, "
            f"extra={sorted(actual_names - expected_names)}"
        )
    if set(sums) != expected_names - {"SHA256SUMS"}:
        raise ValueError("SHA256SUMS file set does not match release manifest")
    for name, expected_digest in sums.items():
        actual_digest = _sha256(root / name)[1]
        if actual_digest != expected_digest:
            raise ValueError(f"checksum mismatch: {name}")
    for name, record in files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(record, dict)
            or set(record) != {"bytes", "sha256", "format"}
            or not isinstance(record.get("bytes"), int)
            or record["bytes"] < 0
            or not isinstance(record.get("format"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256")))
        ):
            raise ValueError(f"invalid release file record: {name}")
        byte_size, digest = _sha256(root / name)
        if byte_size != record.get("bytes") or digest != record.get("sha256"):
            raise ValueError(f"manifest artifact mismatch: {name}")

    version = manifest.get("dataset_version")
    records = manifest.get("record_count")
    if not isinstance(version, str) or not isinstance(records, int):
        raise ValueError("release manifest has invalid version/count")
    sqlite_name = next(
        (
            name
            for name, record in files.items()
            if record.get("format") == _SCHEMA_VERSION
        ),
        None,
    )
    csv_name = next(
        (name for name, record in files.items() if record.get("format") == _CSV_FORMAT),
        None,
    )
    jsonl_name = next(
        (
            name
            for name, record in files.items()
            if record.get("format") == _JSONL_FORMAT
        ),
        None,
    )
    if sqlite_name is None or csv_name is None or jsonl_name is None:
        raise ValueError("release is missing a required data artifact")
    _version, _records, metadata = _validate_database(
        root / sqlite_name, expected_version=version, expected_records=records
    )
    _verify_sorted_export(
        root / csv_name,
        expected_records=records,
        file_format=_CSV_FORMAT,
        database_path=root / sqlite_name,
    )
    _verify_sorted_export(
        root / jsonl_name,
        expected_records=records,
        file_format=_JSONL_FORMAT,
        database_path=root / sqlite_name,
    )

    build_manifest = _load_json(root / "build-manifest.json", _BUILD_MANIFEST_FORMAT)
    quality = _load_json(root / "quality-report.json", _QUALITY_REPORT_FORMAT)
    source_lock = _load_json(root / "source-lock.json", "opencepgeo-source-lock-v1")
    notice = (root / "NOTICE.md").read_text(encoding="utf-8")
    if build_manifest.get("dataset_version") != version:
        raise ValueError("packaged build manifest version mismatch")
    configuration = build_manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("packaged build manifest has no configuration")
    packaged_configs = (
        ("enrichment-config.json", "opencepgeo-enrichment-v1", "enrichment"),
        ("quality-policy.json", _QUALITY_POLICY_FORMAT, "quality"),
    )
    for filename, file_format, record_name in packaged_configs:
        _load_json(root / filename, file_format)
        record = configuration.get(record_name)
        if not isinstance(record, dict) or _sha256(root / filename)[1] != record.get(
            "sha256"
        ):
            raise ValueError(f"packaged {record_name} config mismatch")
    corrections_record = configuration.get("opencep_corrections")
    if corrections_record is not None:
        _load_json(root / "opencep-corrections.json", "opencepgeo-corrections-v1")
        if not isinstance(corrections_record, dict) or _sha256(
            root / "opencep-corrections.json"
        )[1] != corrections_record.get("sha256"):
            raise ValueError("packaged corrections mismatch")
    policy = load_quality_policy(root / "quality-policy.json")
    validate_quality_report(
        quality,
        policy,
        expected_dataset_version=version,
        expected_inputs={
            "database_sha256": files[sqlite_name].get("sha256"),
            "build_manifest_sha256": files["build-manifest.json"].get("sha256"),
            "enrichment_config_sha256": files["enrichment-config.json"].get(
                "sha256"
            ),
            "quality_policy_sha256": files["quality-policy.json"].get("sha256"),
        },
    )
    if quality.get("status") != "pass":
        raise ValueError("packaged quality report is failing")
    if (root / "quality-report.md").read_text(
        encoding="utf-8"
    ) != quality_report_markdown(quality):
        raise ValueError("packaged quality Markdown does not match JSON report")
    quality_inputs = quality["inputs"]
    assert isinstance(quality_inputs, dict)
    build_artifacts = build_manifest.get("artifacts")
    if not isinstance(build_artifacts, dict):
        raise ValueError("packaged build manifest has no artifacts")
    build_sqlite = build_artifacts.get("sqlite")
    build_jsonl = build_artifacts.get("normalized")
    if (
        not isinstance(build_sqlite, dict)
        or build_sqlite.get("sha256") != files[sqlite_name].get("sha256")
        or not isinstance(build_jsonl, dict)
        or build_jsonl.get("sha256") != files[jsonl_name].get("sha256")
    ):
        raise ValueError("packaged build manifest does not match data artifacts")
    if source_lock.get("publication_gate") != manifest.get("publication_gate"):
        raise ValueError("packaged publication gate mismatch")
    if _sha256(root / "source-lock.json")[1] != manifest.get("source_lock_sha256"):
        raise ValueError("packaged source-lock checksum mismatch")
    if metadata.get("source_lock_sha256") != manifest.get("source_lock_sha256"):
        raise ValueError("packaged SQLite/source-lock mismatch")
    builder = build_manifest.get("builder")
    if (
        not isinstance(builder, dict)
        or manifest.get("builder") != builder
        or any(
            metadata.get(metadata_key) != builder.get(builder_key)
            for metadata_key, builder_key in (
                ("builder_name", "name"),
                ("builder_version", "version"),
                ("builder_source_tree_sha256", "source_tree_sha256"),
            )
        )
    ):
        raise ValueError("packaged builder identity mismatch")
    attestation = manifest.get("quality_attestation")
    expected_attestation = {
        "mode": "package-time-recomputed-from-manifest-bound-evidence-v1",
        "distribution_verification": "attestation-only-evidence-not-packaged",
        "report_format": _QUALITY_REPORT_FORMAT,
        "report_sha256": files["quality-report.json"].get("sha256"),
        "database_sha256": quality_inputs["database_sha256"],
        "build_manifest_sha256": quality_inputs["build_manifest_sha256"],
        "ibge_sha256": quality_inputs["ibge_sha256"],
        "osm_observations_sha256": quality_inputs["osm_observations_sha256"],
        "official_holdout_sha256": quality_inputs["official_holdout_sha256"],
        "official_holdout_source_id": quality_inputs[
            "official_holdout_source_id"
        ],
        "official_holdout_filename": quality_inputs[
            "official_holdout_filename"
        ],
        "official_holdout_bytes": quality_inputs["official_holdout_bytes"],
        "official_holdout_path_contract": quality_inputs[
            "official_holdout_path_contract"
        ],
        "municipality_boundaries_sha256": quality_inputs[
            "municipality_boundaries_sha256"
        ],
        "enrichment_config_sha256": quality_inputs[
            "enrichment_config_sha256"
        ],
        "quality_policy_sha256": quality_inputs["quality_policy_sha256"],
        "cohort_split": {
            "algorithm": quality["cohorts"]["unseen_cep"]["algorithm"],
            "modulus": quality["cohorts"]["unseen_cep"]["modulus"],
            "remainder": quality["cohorts"]["unseen_cep"]["remainder"],
        },
    }
    if attestation != expected_attestation:
        raise ValueError("packaged quality attestation mismatch")
    required_attribution = manifest.get("required_attribution")
    if not isinstance(required_attribution, list) or any(
        not isinstance(token, str) for token in required_attribution
    ):
        raise ValueError("release manifest has invalid attribution requirements")
    missing_attribution = [
        token for token in required_attribution if token not in notice
    ]
    if missing_attribution:
        raise ValueError(f"NOTICE is missing attribution: {missing_attribution}")
    if manifest.get("release_status") != "blocked-private-release-candidate":
        raise ValueError("release candidate is not publication-blocked")
    return {
        "status": "verified",
        "dataset_version": version,
        "record_count": records,
        "release_status": manifest["release_status"],
        "files": len(files),
    }
