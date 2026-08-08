from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .boundaries import select_municipality_observations
from .config import EnrichmentConfig, load_enrichment_config
from .estimator import CentroidEstimator, normalize_cep, normalize_ibge
from .provenance import builder_identity
from .quality import enforce_build_quality, load_quality_policy
from .source_lock import (
    LockedSource,
    SourceLock,
    load_source_lock,
    repository_source_path,
    verify_file,
)
from .sources import (
    iter_opencep_records,
    load_ibge_municipality_references,
    load_observations,
    load_osm_observations,
)

_SCHEMA_VERSION = "opencepgeo-sqlite-v4"
_EXPORT_FORMAT = "opencepgeo-jsonl-v4"
_MANIFEST_FORMAT = "opencepgeo-build-manifest-v2"

_SCHEMA = f"""
PRAGMA page_size = 4096;
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = OFF;
PRAGMA auto_vacuum = NONE;

CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE cep_geo (
    cep          TEXT PRIMARY KEY CHECK (
        length(cep) = 8 AND cep NOT GLOB '*[^0-9]*'
    ),
    prefix       TEXT NOT NULL CHECK (
        length(prefix) = 5 AND prefix NOT GLOB '*[^0-9]*'
        AND prefix = substr(cep, 1, 5)
    ),
    street       TEXT,
    complement   TEXT,
    unit         TEXT,
    neighborhood TEXT,
    city         TEXT NOT NULL,
    uf           TEXT NOT NULL CHECK (length(uf) = 2),
    state        TEXT,
    region       TEXT,
    ibge         TEXT NOT NULL CHECK (
        length(ibge) = 7 AND ibge NOT GLOB '*[^0-9]*'
    ),
    latitude     REAL,
    longitude    REAL,
    precision    TEXT CHECK (
        precision IS NULL OR precision IN (
            'observed_cep', 'osm_postcode', 'observed_cep_prefix', 'municipality'
        )
    ),
    method          TEXT,
    evidence_count INTEGER CHECK (
        evidence_count IS NULL OR evidence_count > 0
    ),
    evidence_radius_km REAL CHECK (
        evidence_radius_km IS NULL OR evidence_radius_km >= 0
    ),
    geo_source      TEXT CHECK (geo_source IS NULL OR length(geo_source) <= 2048),
    evidence_digest TEXT CHECK (
        evidence_digest IS NULL OR (
            length(evidence_digest) = 71 AND
            substr(evidence_digest, 1, 7) = 'sha256:' AND
            substr(evidence_digest, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    dataset_version TEXT NOT NULL,
    CHECK (
        (latitude IS NULL AND longitude IS NULL AND precision IS NULL
         AND method IS NULL AND evidence_count IS NULL
         AND evidence_radius_km IS NULL AND geo_source IS NULL
         AND evidence_digest IS NULL)
        OR
        (latitude IS NOT NULL AND longitude IS NOT NULL AND precision IS NOT NULL
         AND method IS NOT NULL AND evidence_count IS NOT NULL
         AND evidence_radius_km IS NOT NULL AND geo_source IS NOT NULL
         AND evidence_digest IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE INDEX cep_geo_prefix_ibge_idx ON cep_geo (prefix, ibge);
CREATE INDEX cep_geo_ibge_idx ON cep_geo (ibge);
CREATE INDEX cep_geo_precision_idx ON cep_geo (precision);
"""

_ROW_COLUMNS = (
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


def _text(value: object) -> str | None:
    result = str(value).strip() if value is not None else ""
    return result or None


def _rows(
    records: Iterator[dict[str, object]],
    estimator: CentroidEstimator,
    municipality_codes: set[str],
    counters: dict[str, int],
    dataset_version: str,
) -> Iterator[tuple[object, ...]]:
    for record_number, record in enumerate(records, start=1):
        counters["input_records"] += 1
        cep = normalize_cep(record.get("cep"))
        if cep is None:
            raise ValueError(f"invalid CEP in OpenCEP record {record_number}")
        ibge = normalize_ibge(record.get("ibge"))
        if ibge is None:
            raise ValueError(f"invalid or missing IBGE code for CEP {cep}")
        city = _text(record.get("localidade"))
        uf = _text(record.get("uf"))
        if city is None or uf is None:
            raise ValueError(f"missing city/UF source metadata for CEP {cep}")
        if ibge in municipality_codes:
            counters["ibge_joined"] += 1
        estimate = estimator.estimate(cep, ibge)
        yield (
            cep,
            cep[:5],
            _text(record.get("logradouro")),
            _text(record.get("complemento")),
            _text(record.get("unidade")),
            _text(record.get("bairro")),
            city,
            uf,
            _text(record.get("estado")),
            _text(record.get("regiao")),
            ibge,
            estimate.latitude if estimate else None,
            estimate.longitude if estimate else None,
            estimate.precision if estimate else None,
            estimate.method if estimate else None,
            estimate.evidence_count if estimate else None,
            estimate.evidence_radius_km if estimate else None,
            (
                json.dumps(estimate.sources, ensure_ascii=False, separators=(",", ":"))
                if estimate
                else None
            ),
            estimate.evidence_digest if estimate else None,
            dataset_version,
        )


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


def _temporary_path(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def _locked_source(lock: SourceLock, input_path: str | Path) -> LockedSource:
    filename = Path(input_path).name
    matches = [source for source in lock.sources if source.filename == filename]
    if len(matches) != 1:
        raise ValueError(
            f"source lock has no unique entry for input filename: {filename}"
        )
    source = matches[0]
    verify_file(input_path, source)
    return source


def _source_metadata(
    opencep_path: str | Path,
    ibge_path: str | Path,
    source_version: str | None,
    source_lock_path: str | Path | None,
) -> tuple[str, dict[str, object] | None, list[dict[str, object]], Path | None]:
    if source_lock_path is None:
        if source_version is None or not source_version.strip():
            raise ValueError("source_version or source_lock_path is required")
        return source_version.strip(), None, [], None

    lock = load_source_lock(source_lock_path)
    if source_version is not None and source_version.strip() != lock.release:
        raise ValueError("source_version does not match source lock release")
    selected = [_locked_source(lock, opencep_path), _locked_source(lock, ibge_path)]
    if selected[0].source_id == selected[1].source_id:
        raise ValueError("OpenCEP and IBGE inputs resolve to the same lock entry")
    lock_bytes, lock_sha256 = _sha256(lock.path)
    lock_metadata = {
        "filename": lock.path.name,
        "bytes": lock_bytes,
        "sha256": lock_sha256,
        "publication_gate": json.loads(lock.path.read_text(encoding="utf-8")).get(
            "publication_gate"
        ),
    }
    corrections = selected[0].metadata.get("corrections_source_id")
    corrections_path = None
    if corrections is not None:
        if not isinstance(corrections, str) or not corrections.strip():
            raise ValueError("OpenCEP corrections_source_id must be non-empty")
        matches = [source for source in lock.sources if source.source_id == corrections]
        if len(matches) != 1:
            raise ValueError("OpenCEP correction source is not uniquely locked")
        correction_source = matches[0]
        corrections_path = repository_source_path(lock, correction_source)
        verify_file(corrections_path, correction_source)
        selected.append(correction_source)
    return (
        lock.release,
        lock_metadata,
        [source.metadata for source in selected],
        corrections_path,
    )


def _contract_row(row: sqlite3.Row) -> dict[str, object]:
    record = {column: row[column] for column in _ROW_COLUMNS}
    raw_sources = record.pop("geo_source")
    sources = json.loads(raw_sources) if raw_sources else []
    evidence_digest = record.pop("evidence_digest")
    latitude = record.pop("latitude")
    longitude = record.pop("longitude")
    precision = record.pop("precision")
    method = record.pop("method")
    evidence_count = record.pop("evidence_count")
    evidence_radius_km = record.pop("evidence_radius_km")
    record["geo"] = None
    if latitude is not None and longitude is not None:
        record["geo"] = {
            "type": "Point",
            "coordinates": [longitude, latitude],
            "precision": precision,
            "method": method,
            "evidence_count": evidence_count,
            "evidence_radius_km": evidence_radius_km,
            "source": sources,
            "evidence_digest": evidence_digest,
        }
    return record


def _write_normalized_export(
    connection: sqlite3.Connection, path: Path
) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    connection.row_factory = sqlite3.Row
    with path.open("xb") as output:
        for row in connection.execute("SELECT * FROM cep_geo ORDER BY cep"):
            payload = (
                json.dumps(
                    _contract_row(row),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            output.write(payload)
            digest.update(payload)
            byte_size += len(payload)
        output.flush()
        os.fsync(output.fileno())
    return byte_size, digest.hexdigest()


def _artifact_record(
    path: Path, *, byte_size: int | None = None, sha256: str | None = None
) -> dict[str, object]:
    if byte_size is None or sha256 is None:
        byte_size, sha256 = _sha256(path)
    return {"filename": path.name, "bytes": byte_size, "sha256": sha256}


def _input_record(path: str | Path) -> dict[str, object]:
    source = Path(path)
    if source.is_file():
        return _artifact_record(source)
    if not source.is_dir():
        raise ValueError(f"build input does not exist: {source}")
    digest = hashlib.sha256()
    byte_size = 0
    for child in sorted(
        (candidate for candidate in source.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(source).as_posix(),
    ):
        name = child.relative_to(source).as_posix().encode("utf-8")
        payload = child.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        byte_size += len(payload)
    return {
        "filename": source.name,
        "bytes": byte_size,
        "sha256": digest.hexdigest(),
        "format": "deterministic-directory-tree-v1",
    }


def _observations_metadata(
    path: str | Path | None,
    source_lock_path: str | Path | None,
) -> dict[str, object] | None:
    if path is None:
        return None
    observation_path = Path(path)
    artifact = _artifact_record(observation_path)
    if source_lock_path is not None:
        source = _locked_source(load_source_lock(source_lock_path), observation_path)
        artifact["source"] = source.metadata
    return artifact


def _osm_observations_metadata(
    path: str | Path | None,
    source_lock_metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if path is None:
        return None
    evidence_path = Path(path)
    artifact = _artifact_record(evidence_path)
    manifest_path = evidence_path.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        if source_lock_metadata is not None:
            raise ValueError(f"OSM evidence manifest is missing: {manifest_path}")
        return {"artifact": artifact, "manifest": None}
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid OSM evidence manifest: {exc}") from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != "opencepgeo-osm-evidence-manifest-v1"
    ):
        raise ValueError("unsupported OSM evidence manifest format")
    if document.get("artifact") != artifact:
        raise ValueError("OSM evidence bytes do not match its manifest")
    if source_lock_metadata is not None:
        manifest_lock = document.get("source_lock")
        if not isinstance(manifest_lock, dict) or manifest_lock.get(
            "sha256"
        ) != source_lock_metadata.get("sha256"):
            raise ValueError("OSM evidence source lock does not match the build lock")
    return {
        "artifact": artifact,
        "manifest": _artifact_record(manifest_path),
        "source": document.get("source"),
        "statistics": document.get("statistics"),
        "publication_gate": document.get("publication_gate"),
    }


def _opencep_targets(
    path: str | Path,
    corrections_path: str | Path | None,
    needed_ceps: set[str],
) -> dict[str, str]:
    targets: dict[str, str] = {}
    for record in iter_opencep_records(path, corrections_path):
        cep = normalize_cep(record.get("cep"))
        if cep not in needed_ceps:
            continue
        ibge = normalize_ibge(record.get("ibge"))
        if ibge is None:
            raise ValueError(f"invalid target IBGE for OSM CEP {cep}")
        targets[cep] = ibge
    return targets


def build_database(
    *,
    opencep_path: str | Path,
    ibge_path: str | Path,
    output_path: str | Path,
    source_version: str | None = None,
    source_lock_path: str | Path | None = None,
    observations_path: str | Path | None = None,
    osm_observations_path: str | Path | None = None,
    municipality_boundaries_path: str | Path | None = None,
    enrichment_config_path: str | Path | None = None,
    quality_config_path: str | Path | None = None,
    export_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    min_prefix_samples: int = 3,
    max_prefix_radius_km: float = 25.0,
    force: bool = False,
) -> dict[str, object]:
    dataset_version, lock_metadata, locked_sources, corrections_path = _source_metadata(
        opencep_path, ibge_path, source_version, source_lock_path
    )
    output = Path(output_path)
    export = Path(export_path) if export_path is not None else None
    manifest = Path(manifest_path) if manifest_path is not None else None
    targets = [path for path in (output, export, manifest) if path is not None]
    if len({path.resolve() for path in targets}) != len(targets):
        raise ValueError("output, export, and manifest paths must be distinct")
    for target in targets:
        if target.exists() and not force:
            raise FileExistsError(
                f"output exists; pass --force to replace it: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)

    temporary_output = _temporary_path(output)
    temporary_export = _temporary_path(export) if export is not None else None
    temporary_manifest = _temporary_path(manifest) if manifest is not None else None
    temporary_paths = [
        path
        for path in (temporary_output, temporary_export, temporary_manifest)
        if path is not None
    ]

    if enrichment_config_path is not None:
        enrichment, enrichment_record = load_enrichment_config(enrichment_config_path)
    else:
        enrichment = EnrichmentConfig(
            version="inline-fixture-v1",
            min_prefix_samples=min_prefix_samples,
            max_prefix_radius_km=max_prefix_radius_km,
            max_observed_radius_km=10.0,
            max_osm_radius_km=5.0,
            max_osm_municipality_distance_km=250.0,
            outlier_min_samples=3,
            outlier_mad_multiplier=3.0,
            outlier_floor_km=0.25,
        )
        enrichment_record = None

    if quality_config_path is not None:
        quality_policy = load_quality_policy(quality_config_path)
        quality_record: dict[str, object] | None = {
            "filename": quality_policy.path.name,
            "sha256": quality_policy.sha256,
            "version": quality_policy.version,
        }
    else:
        quality_policy = None
        quality_record = None

    municipalities = load_ibge_municipality_references(ibge_path)
    observations = load_observations(
        observations_path, require_evidence_id=observations_path is not None
    )
    osm_observations = load_osm_observations(osm_observations_path)
    boundary_record = None
    boundary_selection = None
    expected_boundary_members = None
    if osm_observations and source_lock_path is not None and municipality_boundaries_path is None:
        raise ValueError(
            "checksum-locked OSM builds require official municipality boundaries"
        )
    if municipality_boundaries_path is not None:
        boundary_record = _input_record(municipality_boundaries_path)
        if source_lock_path is not None:
            boundary_source = _locked_source(
                load_source_lock(source_lock_path), municipality_boundaries_path
            )
            if all(
                source.get("id") != boundary_source.source_id
                for source in locked_sources
            ):
                locked_sources.append(boundary_source.metadata)
            raw_members = boundary_source.metadata.get("members")
            if not isinstance(raw_members, dict):
                raise ValueError("locked municipality boundaries require member identities")
            expected_boundary_members = raw_members
            boundary_record["members"] = raw_members
        targets = _opencep_targets(
            opencep_path,
            corrections_path,
            {observation.cep for observation in osm_observations},
        )
        selection = select_municipality_observations(
            municipality_boundaries_path,
            osm_observations,
            targets,
            expected_members=expected_boundary_members,
        )
        if (
            len(selection.interior_target_municipality)
            + len(selection.boundary_target_municipality)
            + len(selection.outside_target_municipality)
            + len(selection.unknown_cep)
            != len(osm_observations)
        ):
            raise ValueError("municipality boundary selection counts are inconsistent")
        boundary_selection = {
            "method": "ibge-2024-municipality-polygon-containment-v1",
            "input_observations": len(osm_observations),
            "known_target_observations": (
                len(osm_observations) - len(selection.unknown_cep)
            ),
            "eligible_observations": len(selection.eligible),
            "interior_target_municipality": len(
                selection.interior_target_municipality
            ),
            "boundary_target_municipality": len(
                selection.boundary_target_municipality
            ),
            "excluded_observations": len(selection.outside_target_municipality),
            "excluded_by_reason": {
                "outside_target_municipality": len(
                    selection.outside_target_municipality
                )
            },
            "outside_target_municipality": len(
                selection.outside_target_municipality
            ),
            "unknown_cep": len(selection.unknown_cep),
        }
        osm_observations = list(selection.eligible)
    corrections_record = (
        _artifact_record(corrections_path) if corrections_path else None
    )
    observations_record = _observations_metadata(observations_path, source_lock_path)
    osm_observations_record = _osm_observations_metadata(
        osm_observations_path, lock_metadata
    )
    estimator = CentroidEstimator(
        observations,
        municipalities,
        osm_observations=osm_observations,
        min_prefix_samples=enrichment.min_prefix_samples,
        max_prefix_radius_km=enrichment.max_prefix_radius_km,
        max_observed_radius_km=enrichment.max_observed_radius_km,
        max_osm_radius_km=enrichment.max_osm_radius_km,
        max_osm_municipality_distance_km=enrichment.max_osm_municipality_distance_km,
        outlier_min_samples=enrichment.outlier_min_samples,
        outlier_mad_multiplier=enrichment.outlier_mad_multiplier,
        outlier_floor_km=enrichment.outlier_floor_km,
    )
    counters = {"input_records": 0, "ibge_joined": 0}

    identity = builder_identity()
    connection = sqlite3.connect(temporary_output)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (
                ("format", _SCHEMA_VERSION),
                ("dataset_version", dataset_version),
                ("builder_name", identity["name"]),
                ("builder_version", identity["version"]),
                ("builder_source_tree_sha256", identity["source_tree_sha256"]),
                ("enrichment_version", enrichment.version),
                ("min_prefix_samples", str(enrichment.min_prefix_samples)),
                ("max_prefix_radius_km", str(enrichment.max_prefix_radius_km)),
            ),
        )
        connection.executemany(
            """
            INSERT INTO cep_geo (
                cep, prefix, street, complement, unit, neighborhood,
                city, uf, state, region, ibge, latitude, longitude,
                precision, method, evidence_count, evidence_radius_km, geo_source,
                evidence_digest, dataset_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _rows(
                iter_opencep_records(opencep_path, corrections_path),
                estimator,
                set(municipalities),
                counters,
                dataset_version,
            ),
        )
        connection.commit()
        unique_ceps = connection.execute("SELECT count(*) FROM cep_geo").fetchone()[0]
        if unique_ceps != counters["input_records"]:
            raise ValueError(
                f"input/unique CEP count mismatch: {counters['input_records']} vs {unique_ceps}"
            )
        located = connection.execute(
            "SELECT count(*) FROM cep_geo WHERE latitude IS NOT NULL"
        ).fetchone()[0]
        unresolved = unique_ceps - located
        statistics: dict[str, int | float] = {
            "input_records": counters["input_records"],
            "unique_ceps": unique_ceps,
            "ibge_joined": counters["ibge_joined"],
            "ibge_join_rate": round(counters["ibge_joined"] / unique_ceps, 8),
            "located": located,
            "unresolved": unresolved,
        }
        tier_counts = {
            (precision or "unresolved"): count
            for precision, count in connection.execute(
                "SELECT precision, count(*) FROM cep_geo GROUP BY precision"
            )
        }
        for precision in (
            "observed_cep",
            "osm_postcode",
            "observed_cep_prefix",
            "municipality",
            "unresolved",
        ):
            statistics[f"tier_{precision}"] = tier_counts.get(precision, 0)

        if quality_policy is not None:
            enforce_build_quality(connection, quality_policy)

        export_record = None
        if temporary_export is not None and export is not None:
            export_bytes, export_sha256 = _write_normalized_export(
                connection, temporary_export
            )
            export_record = _artifact_record(
                export, byte_size=export_bytes, sha256=export_sha256
            )
            export_record["format"] = _EXPORT_FORMAT
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("normalized_export_sha256", export_sha256),
            )

        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ((f"count_{key}", str(value)) for key, value in statistics.items()),
        )
        if lock_metadata is not None:
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("source_lock_sha256", str(lock_metadata["sha256"])),
            )
        if corrections_record is not None:
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("opencep_corrections_sha256", str(corrections_record["sha256"])),
            )
        if enrichment_record is not None:
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("enrichment_config_sha256", str(enrichment_record["sha256"])),
            )
        if quality_record is not None:
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                (
                    ("quality_config_sha256", str(quality_record["sha256"])),
                    ("quality_version", str(quality_record["version"])),
                ),
            )
        connection.commit()
        connection.execute("VACUUM")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed before promotion: {integrity}")
        connection.close()
        connection = None

        sqlite_record = _artifact_record(temporary_output)
        sqlite_record["filename"] = output.name
        if temporary_manifest is not None and manifest is not None:
            manifest_document: dict[str, object] = {
                "format": _MANIFEST_FORMAT,
                "schema_version": _SCHEMA_VERSION,
                "dataset_version": dataset_version,
                "builder": identity,
                "inputs": {
                    "opencep": _input_record(opencep_path),
                    "ibge": _input_record(ibge_path),
                    "municipality_boundaries": boundary_record,
                },
                "configuration": {
                    "enrichment": enrichment_record or enrichment.as_dict(),
                    "quality": quality_record,
                    "observations": observations_record,
                    "osm_observations": osm_observations_record,
                    "osm_boundary_selection": boundary_selection,
                    "opencep_corrections": corrections_record,
                },
                "source_lock": lock_metadata,
                "sources": locked_sources,
                "statistics": statistics,
                "artifacts": {
                    "sqlite": sqlite_record,
                    "normalized": export_record,
                },
            }
            payload = (
                json.dumps(
                    manifest_document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            with temporary_manifest.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

        if temporary_export is not None and export is not None:
            os.replace(temporary_export, export)
            temporary_paths.remove(temporary_export)
        os.replace(temporary_output, output)
        temporary_paths.remove(temporary_output)
        if temporary_manifest is not None and manifest is not None:
            os.replace(temporary_manifest, manifest)
            temporary_paths.remove(temporary_manifest)
        return {
            **statistics,
            "sqlite_sha256": sqlite_record["sha256"],
            "normalized_sha256": export_record["sha256"] if export_record else None,
        }
    except Exception:
        if connection is not None:
            connection.close()
        raise
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
            temporary_path.with_name(f"{temporary_path.name}-wal").unlink(
                missing_ok=True
            )
            temporary_path.with_name(f"{temporary_path.name}-shm").unlink(
                missing_ok=True
            )


def lookup(database_path: str | Path, cep: object) -> dict[str, object] | None:
    cep8 = normalize_cep(cep)
    if cep8 is None:
        return None
    connection = sqlite3.connect(
        f"file:{Path(database_path).resolve()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("format") != _SCHEMA_VERSION:
            raise ValueError(
                f"incompatible SQLite schema: {metadata.get('format')!r}; "
                f"expected {_SCHEMA_VERSION!r}"
            )
        row = connection.execute(
            "SELECT * FROM cep_geo WHERE cep = ?", (cep8,)
        ).fetchone()
    finally:
        connection.close()
    return _contract_row(row) if row is not None else None
