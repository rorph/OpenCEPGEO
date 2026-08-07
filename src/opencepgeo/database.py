from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .estimator import CentroidEstimator, normalize_cep, normalize_ibge
from .sources import (
    iter_opencep_records,
    load_ibge_municipality_points,
    load_observations,
)

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE cep_geo (
    cep          TEXT PRIMARY KEY,
    prefix       TEXT NOT NULL,
    street       TEXT,
    complement   TEXT,
    unit         TEXT,
    neighborhood TEXT,
    city         TEXT,
    uf           TEXT,
    state        TEXT,
    region       TEXT,
    ibge         TEXT,
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
);

CREATE INDEX cep_geo_prefix_ibge_idx ON cep_geo (prefix, ibge);
CREATE INDEX cep_geo_ibge_idx ON cep_geo (ibge);
CREATE INDEX cep_geo_precision_idx ON cep_geo (precision);
"""


def _text(value: object) -> str | None:
    result = str(value).strip() if value is not None else ""
    return result or None


def _rows(
    records: Iterator[dict[str, object]], estimator: CentroidEstimator
) -> Iterator[tuple[object, ...]]:
    for record in records:
        cep = normalize_cep(record.get("cep"))
        if cep is None:
            continue
        ibge = normalize_ibge(record.get("ibge"))
        estimate = estimator.estimate(cep, ibge)
        yield (
            cep,
            cep[:5],
            _text(record.get("logradouro")),
            _text(record.get("complemento")),
            _text(record.get("unidade")),
            _text(record.get("bairro")),
            _text(record.get("localidade")),
            _text(record.get("uf")),
            _text(record.get("estado")),
            _text(record.get("regiao")),
            ibge,
            estimate.latitude if estimate else None,
            estimate.longitude if estimate else None,
            estimate.precision if estimate else None,
            estimate.sample_size if estimate else None,
            estimate.radius_km if estimate else None,
            json.dumps(estimate.sources, ensure_ascii=False) if estimate else None,
        )


def build_database(
    *,
    opencep_path: str | Path,
    ibge_path: str | Path,
    output_path: str | Path,
    source_version: str,
    observations_path: str | Path | None = None,
    min_prefix_samples: int = 3,
    max_prefix_radius_km: float = 25.0,
    force: bool = False,
) -> dict[str, int]:
    if not source_version.strip():
        raise ValueError("source_version must not be empty")
    output = Path(output_path)
    if output.exists() and not force:
        raise FileExistsError(f"output exists; pass --force to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists():
        temporary.unlink()

    municipalities = load_ibge_municipality_points(ibge_path)
    observations = load_observations(observations_path)
    estimator = CentroidEstimator(
        observations,
        municipalities,
        min_prefix_samples=min_prefix_samples,
        max_prefix_radius_km=max_prefix_radius_km,
    )

    connection = sqlite3.connect(temporary)
    try:
        connection.executescript(_SCHEMA)
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            (
                ("format", "opencepgeo-sqlite-v1"),
                ("source_version", source_version),
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
            _rows(iter_opencep_records(opencep_path), estimator),
        )
        connection.commit()
        stats = {
            "rows": connection.execute("SELECT count(*) FROM cep_geo").fetchone()[0],
            "located": connection.execute(
                "SELECT count(*) FROM cep_geo WHERE latitude IS NOT NULL"
            ).fetchone()[0],
            "unresolved": connection.execute(
                "SELECT count(*) FROM cep_geo WHERE latitude IS NULL"
            ).fetchone()[0],
        }
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        connection.close()
        if temporary.exists():
            temporary.unlink()
        raise
    else:
        connection.close()
        os.replace(temporary, output)
        return stats


def lookup(database_path: str | Path, cep: object) -> dict[str, object] | None:
    cep8 = normalize_cep(cep)
    if cep8 is None:
        return None
    connection = sqlite3.connect(f"file:{Path(database_path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM cep_geo WHERE cep = ?", (cep8,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    result = dict(row)
    sources = json.loads(result.pop("geo_source")) if result.get("geo_source") else []
    latitude = result.pop("latitude")
    longitude = result.pop("longitude")
    precision = result.pop("precision")
    sample_size = result.pop("sample_size")
    radius_km = result.pop("radius_km")
    result["geo"] = None
    if latitude is not None and longitude is not None:
        result["geo"] = {
            "type": "Point",
            "coordinates": [longitude, latitude],
            "precision": precision,
            "sample_size": sample_size,
            "radius_km": radius_km,
            "source": sources,
        }
    return result
