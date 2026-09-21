"""Overlay pinned CNEFE 2022 centroids onto an inherited OpenCEPGeo SQLite.

Phase 3b of PIN-221: accepted CNEFE `observed_cep` points replace a coarser
inherited coordinate only when they match the pinned candidate sqlite exactly
(CEP, IBGE, lat/lon, provenance). The overlay never writes a worse precision
tier and never mutates the inherited file.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

_PRECISION_RANK = {
    "municipality": 0,
    "observed_cep_prefix": 1,
    "osm_postcode": 2,
    "observed_cep": 3,
}
_CNEFE_PRECISION = "observed_cep"
_CNEFE_METHOD = "cnefe_robust_median"
_DIGEST_RE_PREFIX = "sha256:"


@dataclass(frozen=True)
class CnefePoint:
    cep: str
    latitude: float
    longitude: float
    ibge: str
    evidence_count: int
    evidence_radius_km: float
    geo_source: str
    evidence_digest: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_accepted_cnefe_points(path: Path) -> dict[str, CnefePoint]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT cep, latitude, longitude, ibge, evidence_count,
                   evidence_radius_km, sources, evidence_digest
            FROM candidate
            WHERE status = 'accepted'
              AND precision = 'observed_cep'
              AND method = 'cnefe_robust_median'
            """
        )
        accepted: dict[str, CnefePoint] = {}
        for (
            cep,
            latitude,
            longitude,
            ibge,
            evidence_count,
            evidence_radius_km,
            sources,
            evidence_digest,
        ) in rows:
            if not isinstance(cep, str) or len(cep) != 8 or not cep.isdigit():
                raise ValueError(f"invalid CNEFE CEP {cep!r}")
            if not isinstance(ibge, str) or len(ibge) != 7 or not ibge.isdigit():
                raise ValueError(f"invalid CNEFE IBGE for {cep}")
            if not isinstance(evidence_digest, str) or not evidence_digest.startswith(
                _DIGEST_RE_PREFIX
            ):
                raise ValueError(f"invalid CNEFE digest for {cep}")
            if not isinstance(sources, str) or not sources.startswith("["):
                raise ValueError(f"invalid CNEFE sources for {cep}")
            accepted[cep] = CnefePoint(
                cep=cep,
                latitude=float(latitude),
                longitude=float(longitude),
                ibge=ibge,
                evidence_count=int(evidence_count),
                evidence_radius_km=float(evidence_radius_km),
                geo_source=sources,
                evidence_digest=evidence_digest,
            )
    finally:
        connection.close()
    if not accepted:
        raise ValueError("CNEFE candidate sqlite has no accepted observed_cep rows")
    return accepted


def overlay_cnefe_candidate(
    *,
    inherited_path: Path,
    cnefe_path: Path,
    output_path: Path,
    manifest_path: Path,
    dataset_version: str,
    force: bool = False,
) -> dict[str, object]:
    inherited_path = inherited_path.resolve()
    cnefe_path = cnefe_path.resolve()
    output_path = output_path.resolve()
    manifest_path = manifest_path.resolve()
    if output_path.exists() and not force:
        raise FileExistsError(f"{output_path} already exists")
    if output_path == inherited_path:
        raise ValueError("CNEFE overlay refuses to replace the inherited sqlite")
    accepted = load_accepted_cnefe_points(cnefe_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    staging_directory = Path(
        tempfile.mkdtemp(prefix="opencepgeo-cnefe-", dir=str(output_path.parent))
    )
    staging = staging_directory / "candidate.sqlite"
    try:
        shutil.copy2(inherited_path, staging)
        os.chmod(staging, 0o644)
        stats = _apply_overlay(staging, accepted)
        _rewrite_metadata(staging, dataset_version, stats)
        _verify_overlay(staging, accepted, stats)
        os.replace(staging, output_path)
        os.chmod(output_path, 0o444)
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)

    manifest = {
        "format": "opencepgeo-cnefe-overlay-v1",
        "status": "offline-candidate-not-approved-for-promotion",
        "dataset_version": dataset_version,
        "inherited_sqlite_sha256": _sha256_file(inherited_path),
        "cnefe_sqlite_sha256": _sha256_file(cnefe_path),
        "output_sqlite_sha256": _sha256_file(output_path),
        "cnefe_accepted_rows": len(accepted),
        **stats,
        "policy": "upgrade-coarser-tiers-from-pinned-cnefe-v1",
    }
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_path.write_text(encoded, encoding="utf-8")
    os.chmod(manifest_path, 0o444)
    return manifest


def _apply_overlay(
    sqlite_path: Path, accepted: dict[str, CnefePoint]
) -> dict[str, int]:
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        updates: list[tuple[object, ...]] = []
        skipped_missing = 0
        skipped_ibge = 0
        skipped_rank = 0
        for point in accepted.values():
            row = connection.execute(
                "SELECT precision, ibge FROM cep_geo WHERE cep = ?",
                (point.cep,),
            ).fetchone()
            if row is None:
                skipped_missing += 1
                continue
            precision, ibge = row
            if ibge != point.ibge:
                skipped_ibge += 1
                continue
            if _PRECISION_RANK.get(precision, -1) >= _PRECISION_RANK[_CNEFE_PRECISION]:
                skipped_rank += 1
                continue
            updates.append(
                (
                    point.latitude,
                    point.longitude,
                    _CNEFE_PRECISION,
                    _CNEFE_METHOD,
                    point.evidence_count,
                    point.evidence_radius_km,
                    point.geo_source,
                    point.evidence_digest,
                    point.cep,
                )
            )
        connection.executemany(
            """
            UPDATE cep_geo
            SET latitude = ?,
                longitude = ?,
                precision = ?,
                method = ?,
                evidence_count = ?,
                evidence_radius_km = ?,
                geo_source = ?,
                evidence_digest = ?
            WHERE cep = ?
            """,
            updates,
        )
        if connection.total_changes != len(updates):
            raise ValueError(
                "CNEFE overlay update count disagreed with planned upgrades"
            )
        connection.commit()
    finally:
        connection.close()
    return {
        "upgraded": len(updates),
        "skipped_missing_from_inherited": skipped_missing,
        "skipped_ibge_mismatch": skipped_ibge,
        "skipped_same_or_better_tier": skipped_rank,
    }


def _rewrite_metadata(
    sqlite_path: Path, dataset_version: str, stats: dict[str, int]
) -> None:
    connection = sqlite3.connect(sqlite_path)
    try:
        counts = dict(
            connection.execute(
                "SELECT precision, count(*) FROM cep_geo GROUP BY precision"
            )
        )
        unique = connection.execute("SELECT count(*) FROM cep_geo").fetchone()[0]
        located = connection.execute(
            "SELECT count(*) FROM cep_geo WHERE latitude IS NOT NULL"
        ).fetchone()[0]
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            ("dataset_version", dataset_version),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            ("cnefe_overlay_upgraded", str(stats["upgraded"])),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            ("count_unique_ceps", str(unique)),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            ("count_located", str(located)),
        )
        for key, precision in (
            ("count_tier_municipality", "municipality"),
            ("count_tier_osm_postcode", "osm_postcode"),
            ("count_tier_observed_cep", "observed_cep"),
            ("count_tier_observed_cep_prefix", "observed_cep_prefix"),
        ):
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (key, str(int(counts.get(precision, 0)))),
            )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()


def _verify_overlay(
    sqlite_path: Path,
    accepted: dict[str, CnefePoint],
    stats: dict[str, int],
) -> None:
    connection = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    try:
        observed = 0
        for (
            cep,
            precision,
            method,
            latitude,
            longitude,
            ibge,
            digest,
        ) in connection.execute(
            """
            SELECT cep, precision, method, latitude, longitude, ibge, evidence_digest
            FROM cep_geo
            WHERE precision = 'observed_cep' AND method = 'cnefe_robust_median'
            """
        ):
            point = accepted.get(cep)
            if point is None:
                raise ValueError(
                    f"{cep} has a CNEFE overlay geo without pinned evidence"
                )
            if (
                point.ibge != ibge
                or point.latitude != float(latitude)
                or point.longitude != float(longitude)
                or point.evidence_digest != digest
            ):
                raise ValueError(
                    f"{cep} overlay coordinate does not match pinned CNEFE evidence"
                )
            observed += 1
        if observed != stats["upgraded"]:
            raise ValueError(
                "verified CNEFE overlay rows "
                f"{observed} != planned upgrades {stats['upgraded']}"
            )
        named = connection.execute(
            "SELECT precision, method, latitude, longitude FROM cep_geo WHERE cep = ?",
            ("57920000",),
        ).fetchone()
        if named is not None and "57920000" in accepted:
            precision, method, latitude, longitude = named
            if precision != _CNEFE_PRECISION or method != _CNEFE_METHOD:
                raise ValueError(
                    "57920000 did not upgrade to pinned CNEFE observed_cep"
                )
    finally:
        connection.close()
