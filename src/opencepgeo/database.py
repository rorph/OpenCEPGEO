from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .estimator import CentroidEstimator, normalize_cep, normalize_ibge
from .source_lock import LockedSource, SourceLock, load_source_lock, verify_file
from .sources import (
    iter_opencep_records,
    load_ibge_municipality_points,
    load_observations,
)

_SCHEMA_VERSION = "opencepgeo-sqlite-v1"
_EXPORT_FORMAT = "opencepgeo-jsonl-v1"
_MANIFEST_FORMAT = "opencepgeo-build-manifest-v1"

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
    cep          TEXT PRIMARY KEY,
    prefix       TEXT NOT NULL,
    street       TEXT,
    complement   TEXT,
    unit         TEXT,
    neighborhood TEXT,
    city         TEXT NOT NULL,
    uf           TEXT NOT NULL,
    state        TEXT,
    region       TEXT,
    ibge         TEXT NOT NULL,
    latitude     REAL,
    longitude    REAL,
    precision    TEXT CHECK (
        precision IS NULL OR precision IN (
            'observed_cep', 'observed_cep_prefix', 'municipality'
        )
    ),
    sample_size  INTEGER,
    radius_km    REAL,
    geo_source   TEXT
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
    "sample_size",
    "radius_km",
    "geo_source",
)


def _text(value: object) -> str | None:
    result = str(value).strip() if value is not None else ""
    return result or None


def _rows(
    records: Iterator[dict[str, object]],
    estimator: CentroidEstimator,
    municipality_codes: set[str],
    counters: dict[str, int],
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
            estimate.sample_size if estimate else None,
            estimate.radius_km if estimate else None,
            (
                json.dumps(estimate.sources, ensure_ascii=False, separators=(",", ":"))
                if estimate
                else None
            ),
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
    corrections = selected[0].metadata.get("corrections")
    corrections_path = None
    if corrections is not None:
        if not isinstance(corrections, str) or Path(corrections).name != corrections:
            raise ValueError(
                "OpenCEP correction path in source lock must be a basename"
            )
        corrections_path = lock.path.parent / corrections
    return (
        lock.release,
        lock_metadata,
        [source.metadata for source in selected],
        corrections_path,
    )


def _contract_row(row: sqlite3.Row) -> dict[str, object]:
    record = {column: row[column] for column in _ROW_COLUMNS}
    sources = json.loads(record.pop("geo_source")) if record.get("geo_source") else []
    latitude = record.pop("latitude")
    longitude = record.pop("longitude")
    precision = record.pop("precision")
    sample_size = record.pop("sample_size")
    radius_km = record.pop("radius_km")
    record["geo"] = None
    if latitude is not None and longitude is not None:
        record["geo"] = {
            "type": "Point",
            "coordinates": [longitude, latitude],
            "precision": precision,
            "sample_size": sample_size,
            "radius_km": radius_km,
            "source": sources,
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


def build_database(
    *,
    opencep_path: str | Path,
    ibge_path: str | Path,
    output_path: str | Path,
    source_version: str | None = None,
    source_lock_path: str | Path | None = None,
    observations_path: str | Path | None = None,
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

    municipalities = load_ibge_municipality_points(ibge_path)
    observations = load_observations(observations_path)
    corrections_record = (
        _artifact_record(corrections_path) if corrections_path else None
    )
    estimator = CentroidEstimator(
        observations,
        municipalities,
        min_prefix_samples=min_prefix_samples,
        max_prefix_radius_km=max_prefix_radius_km,
    )
    counters = {"input_records": 0, "ibge_joined": 0}

    connection = sqlite3.connect(temporary_output)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (
                ("format", _SCHEMA_VERSION),
                ("dataset_version", dataset_version),
                ("min_prefix_samples", str(min_prefix_samples)),
                ("max_prefix_radius_km", str(max_prefix_radius_km)),
            ),
        )
        connection.executemany(
            """
            INSERT INTO cep_geo (
                cep, prefix, street, complement, unit, neighborhood,
                city, uf, state, region, ibge, latitude, longitude,
                precision, sample_size, radius_km, geo_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _rows(
                iter_opencep_records(opencep_path, corrections_path),
                estimator,
                set(municipalities),
                counters,
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
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        connection = None

        sqlite_record = _artifact_record(temporary_output)
        sqlite_record["filename"] = output.name
        if temporary_manifest is not None and manifest is not None:
            manifest_document: dict[str, object] = {
                "format": _MANIFEST_FORMAT,
                "schema_version": _SCHEMA_VERSION,
                "dataset_version": dataset_version,
                "configuration": {
                    "min_prefix_samples": min_prefix_samples,
                    "max_prefix_radius_km": max_prefix_radius_km,
                    "observations": (
                        Path(observations_path).name if observations_path else None
                    ),
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
        row = connection.execute(
            "SELECT * FROM cep_geo WHERE cep = ?", (cep8,)
        ).fetchone()
    finally:
        connection.close()
    return _contract_row(row) if row is not None else None
