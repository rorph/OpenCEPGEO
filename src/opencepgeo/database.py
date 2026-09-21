from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from statistics import median

import fcntl

from .boundaries import select_municipality_observations
from .config import EnrichmentConfig, load_enrichment_config
from .estimator import (
    CentroidEstimator,
    haversine_km,
    normalize_cep,
    normalize_ibge,
)
from .model import Observation, Point
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
    load_ibge_administrative_locality_references,
    load_ibge_municipality_references,
    load_observations,
    load_osm_observations,
)

_SCHEMA_VERSION = "opencepgeo-sqlite-v4"
SCHEMA_VERSION = _SCHEMA_VERSION
_EXPORT_FORMAT = "opencepgeo-jsonl-v4"
_MANIFEST_FORMAT = "opencepgeo-build-manifest-v2"
_REFRESH_MANIFEST_FORMAT = "opencepgeo-correios-refresh-manifest-v1"
_REFRESH_MANIFEST_STATUS = "offline-candidate-not-approved-for-promotion"
_MAX_NORMALIZED_LINE_BYTES = 65_536
_MAX_REFRESH_MANIFEST_BYTES = 4 * 1024 * 1024
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CEP_RE = re.compile(r"\d{8}")
_IBGE_RE = re.compile(r"\d{7}")
_EVIDENCE_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_BRAZIL_UFS = frozenset(
    {
        "AC",
        "AL",
        "AP",
        "AM",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MT",
        "MS",
        "MG",
        "PA",
        "PB",
        "PR",
        "PE",
        "PI",
        "RJ",
        "RN",
        "RS",
        "RO",
        "RR",
        "SC",
        "SP",
        "SE",
        "TO",
    }
)
_NORMALIZED_KEYS = frozenset(
    {
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
        "dataset_version",
        "geo",
    }
)
_GEO_KEYS = frozenset(
    {
        "type",
        "coordinates",
        "precision",
        "method",
        "evidence_count",
        "evidence_radius_km",
        "source",
        "evidence_digest",
    }
)
_PRECISIONS = frozenset(
    {"observed_cep", "osm_postcode", "observed_cep_prefix", "municipality"}
)
_STRING_LIMITS = {
    "street": 4096,
    "complement": 4096,
    "unit": 1024,
    "neighborhood": 4096,
    "city": 512,
    "state": 512,
    "region": 512,
}
_CLASSIFICATION_KEYS = frozenset(
    {
        "added",
        "address_changed",
        "ibge_changed",
        "missing_from_source",
        "source_link_conflict",
        "unchanged",
    }
)
_REFRESH_PRECISION_KEYS = frozenset({"municipality", "osm_postcode"})
# PIN-216: every non-null candidate coordinate must be either byte-identical to
# the inherited release (already proven when that release built) or reproduced
# exactly by recomputing from the pinned IBGE/OSM evidence. Those inputs are
# hash-locked to the inherited lineage, so a point that is neither preserved nor
# reproduced has been moved off its evidence and fails the build.
_COORDINATE_EVIDENCE_POLICY = "preserve-or-reproduce-from-pinned-ibge-osm-v1"
_GEOGRAPHY_ACTION_KEYS = frozenset(
    {
        "assigned_municipality",
        "invalidated_exact_to_municipality",
        "preserved",
        "reassigned_municipality",
        "retained_missing",
        "retained_source_link_conflict",
        "unresolved",
    }
)
_DIFF_KEYS = frozenset(
    {
        "candidate_included",
        "cep",
        "changed_fields",
        "classification",
        "current_ibge",
        "geography_action",
        "previous_cep",
        "previous_ibge",
        "valid_until",
    }
)
_CLASSIFICATION_ACTIONS = {
    "added": frozenset({"assigned_municipality", "unresolved"}),
    "address_changed": frozenset({"preserved", "invalidated_exact_to_municipality"}),
    "ibge_changed": frozenset({"reassigned_municipality"}),
    "missing_from_source": frozenset({"retained_missing"}),
    "source_link_conflict": frozenset({"retained_source_link_conflict"}),
    "unchanged": frozenset({"preserved"}),
}
_CEP_TYPE_KEYS = frozenset({"1", "2", "3", "4", "5", "6"})
_VALIDITY_KEYS = frozenset({"active", "expired"})
_IBGE_RESOLUTION_KEYS = frozenset(
    {
        "cep_unidade_operacional",
        "direct",
        "numero_localidade_superior",
        "numero_localidade_superior+cep_unidade_operacional",
        "source_link_conflict",
        "unresolved",
    }
)
_PRICE_INDEX_CONTRACT_KEYS = frozenset(
    {
        "format",
        "manifest_format",
        "schema_version",
        "artifact_format",
        "evidence_radius_field",
        "normalized_artifact_path",
        "statistics_path",
        "sources_path",
        "source_lock_path",
        "builder_path",
        "quality_path",
        "builder_required_keys",
        "quality_required_keys",
        "quality_status_field",
        "quality_pass_value",
        "coordinate_bounds",
        "require_city",
        "require_ibge",
        "source_category_pattern",
        "source_category_max_utf8_bytes",
        "source_categories_sorted",
        "max_jsonl_line_bytes",
        "max_geo_source_count",
        "max_geo_source_serialized_bytes",
        "row_keys",
        "string_byte_limits",
        "approved_release",
    }
)

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
LOOKUP_COLUMNS = _ROW_COLUMNS
_PREFIX_EXACT_PRECISIONS = frozenset({"observed_cep", "osm_postcode"})
_PREFIX_MIN_EVIDENCE = 3
_PREFIX_MAX_EVIDENCE_RADIUS_KM = 10.0
_PREFIX_MAX_MEMBERS = 1000


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


def _regular_file_identity(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open {label}: {path}: {exc}") from exc
    digest = hashlib.sha256()
    byte_size = 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                byte_size += len(chunk)
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {"filename": path.name, "bytes": byte_size, "sha256": digest.hexdigest()}


def _snapshot_regular_file(
    source: Path, destination: Path, label: str
) -> dict[str, object]:
    """Copy one immutable input from a verified open descriptor into private staging."""
    if source.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {source}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"cannot open {label}: {source}: {exc}") from exc
    digest = hashlib.sha256()
    byte_size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{label} must be a regular file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            os.fdopen(descriptor, "rb") as input_handle,
            destination.open("xb") as output_handle,
        ):
            descriptor = -1
            for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                byte_size += len(chunk)
                digest.update(chunk)
                output_handle.write(chunk)
            after = os.fstat(input_handle.fileno())
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
            or byte_size != after.st_size
        ):
            raise ValueError(f"{label} changed while it was being snapshotted")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "filename": source.name,
        "bytes": byte_size,
        "sha256": digest.hexdigest(),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _validate_normalized_geo(value: object, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != _GEO_KEYS:
        raise ValueError(f"{label} has an invalid geo object")
    coordinates = value.get("coordinates")
    if (
        value.get("type") != "Point"
        or not isinstance(coordinates, list)
        or len(coordinates) != 2
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in coordinates
        )
    ):
        raise ValueError(f"{label} has invalid point coordinates")
    longitude, latitude = coordinates
    if (
        not math.isfinite(float(longitude))
        or not math.isfinite(float(latitude))
        or not -74.0 <= float(longitude) <= -28.0
        or not -34.0 <= float(latitude) <= 5.5
    ):
        raise ValueError(f"{label} has coordinates outside Brazil")
    if value.get("precision") not in _PRECISIONS:
        raise ValueError(f"{label} has invalid precision")
    method = value.get("method")
    if (
        not isinstance(method, str)
        or not method.strip()
        or len(method.encode("utf-8")) > 128
    ):
        raise ValueError(f"{label} has invalid geo method")
    evidence_count = value.get("evidence_count")
    if (
        isinstance(evidence_count, bool)
        or not isinstance(evidence_count, int)
        or evidence_count < 1
    ):
        raise ValueError(f"{label} has invalid evidence count")
    radius = value.get("evidence_radius_km")
    if (
        isinstance(radius, bool)
        or not isinstance(radius, (int, float))
        or not math.isfinite(float(radius))
        or radius < 0
    ):
        raise ValueError(f"{label} has invalid evidence radius")
    sources = value.get("source")
    if (
        not isinstance(sources, list)
        or not 1 <= len(sources) <= 16
        or any(
            not isinstance(source, str) or _SOURCE_RE.fullmatch(source) is None
            for source in sources
        )
        or sources != sorted(set(sources))
        or len(_canonical_json(sources)) > 2048
    ):
        raise ValueError(f"{label} has invalid geo sources")
    evidence_digest = value.get("evidence_digest")
    if (
        not isinstance(evidence_digest, str)
        or _EVIDENCE_RE.fullmatch(evidence_digest) is None
    ):
        raise ValueError(f"{label} has invalid evidence digest")


def _validate_normalized_row(
    row: object, line_number: int, dataset_version: str
) -> dict[str, object]:
    label = f"normalized line {line_number}"
    if not isinstance(row, dict) or set(row) != _NORMALIZED_KEYS:
        raise ValueError(f"{label} has invalid top-level keys")
    cep = row.get("cep")
    if not isinstance(cep, str) or _CEP_RE.fullmatch(cep) is None:
        raise ValueError(f"{label} has an invalid CEP")
    if row.get("prefix") != cep[:5]:
        raise ValueError(f"{label} has a mismatched prefix")
    if row.get("dataset_version") != dataset_version:
        raise ValueError(f"{label} has a mismatched dataset version")
    ibge = row.get("ibge")
    if not isinstance(ibge, str) or _IBGE_RE.fullmatch(ibge) is None:
        raise ValueError(f"{label} has an invalid IBGE code")
    if row.get("uf") not in _BRAZIL_UFS:
        raise ValueError(f"{label} has an invalid UF")
    city = row.get("city")
    if not isinstance(city, str) or not city.strip():
        raise ValueError(f"{label} has no city")
    for key, limit in _STRING_LIMITS.items():
        value = row.get(key)
        required = key == "city"
        if (required and not isinstance(value, str)) or (
            not required and value is not None and not isinstance(value, str)
        ):
            raise ValueError(f"{label} has an invalid {key}")
        if isinstance(value, str) and len(value.encode("utf-8")) > limit:
            raise ValueError(f"{label} has an oversized {key}")
    _validate_normalized_geo(row.get("geo"), label)
    return row


def _iter_normalized_rows(
    path: Path, dataset_version: str, expected: dict[str, object]
) -> Iterator[dict[str, object]]:
    if path.is_symlink():
        raise ValueError(f"normalized input must not be a symlink: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open normalized input {path}: {exc}") from exc
    digest = hashlib.sha256()
    byte_size = 0
    rows = 0
    previous_cep: str | None = None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"normalized input must be a regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            while raw := handle.readline(_MAX_NORMALIZED_LINE_BYTES + 1):
                rows += 1
                byte_size += len(raw)
                digest.update(raw)
                if len(raw) > _MAX_NORMALIZED_LINE_BYTES:
                    raise ValueError(f"normalized line {rows} exceeds the size limit")
                if not raw.endswith(b"\n"):
                    raise ValueError(
                        "normalized input must end every row with a newline"
                    )
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"normalized line {rows} is invalid JSON") from exc
                row = _validate_normalized_row(value, rows, dataset_version)
                if raw != _canonical_json(row) + b"\n":
                    raise ValueError(f"normalized line {rows} is not canonical JSON")
                cep = str(row["cep"])
                if previous_cep is not None and cep <= previous_cep:
                    raise ValueError(
                        f"normalized CEPs are not strictly increasing at line {rows}"
                    )
                previous_cep = cep
                yield row
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if rows < 1:
        raise ValueError("normalized input must contain at least one row")
    if byte_size != expected.get("bytes") or digest.hexdigest() != expected.get(
        "sha256"
    ):
        raise ValueError("normalized input changed or does not match refresh manifest")


def _stream_refresh_diff(
    path: Path, expected: dict[str, object]
) -> tuple[dict[str, int], dict[str, int]]:
    classification_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    byte_size = 0
    previous_cep: str | None = None
    rows = 0
    with path.open("rb") as handle:
        for rows, raw in enumerate(handle, start=1):
            byte_size += len(raw)
            digest.update(raw)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"refresh diff line {rows} is invalid JSON") from exc
            if not isinstance(value, dict) or set(value) != _DIFF_KEYS:
                raise ValueError(f"refresh diff line {rows} has invalid keys")
            cep = value.get("cep")
            classification = value.get("classification")
            action = value.get("geography_action")
            changed_fields = value.get("changed_fields")
            if (
                not isinstance(cep, str)
                or _CEP_RE.fullmatch(cep) is None
                or (previous_cep is not None and cep <= previous_cep)
                or value.get("candidate_included") is not True
                or classification not in _CLASSIFICATION_KEYS
                or action not in _GEOGRAPHY_ACTION_KEYS
                or action not in _CLASSIFICATION_ACTIONS.get(str(classification), ())
                or not isinstance(changed_fields, list)
                or any(
                    not isinstance(field, str) or not field for field in changed_fields
                )
                or len(changed_fields) != len(set(changed_fields))
                or any(
                    item is not None
                    and (not isinstance(item, str) or pattern.fullmatch(item) is None)
                    for item, pattern in (
                        (value.get("current_ibge"), _IBGE_RE),
                        (value.get("previous_ibge"), _IBGE_RE),
                        (value.get("previous_cep"), _CEP_RE),
                    )
                )
                or (
                    value.get("valid_until") is not None
                    and not isinstance(value.get("valid_until"), str)
                )
            ):
                raise ValueError(f"refresh diff line {rows} is invalid")
            previous_cep = cep
            classification_counts[str(classification)] += 1
            action_counts[str(action)] += 1
    if rows < 1:
        raise ValueError("refresh diff must contain at least one row")
    if byte_size != expected.get("bytes") or digest.hexdigest() != expected.get(
        "sha256"
    ):
        raise ValueError("refresh diff changed or does not match refresh manifest")
    return (
        {key: classification_counts[key] for key in _CLASSIFICATION_KEYS},
        {key: action_counts[key] for key in _GEOGRAPHY_ACTION_KEYS},
    )


def _normalized_sql_row(row: dict[str, object]) -> tuple[object, ...]:
    geo = row["geo"]
    if isinstance(geo, dict):
        longitude, latitude = geo["coordinates"]
        precision = geo["precision"]
        method = geo["method"]
        evidence_count = geo["evidence_count"]
        evidence_radius_km = geo["evidence_radius_km"]
        geo_source = _canonical_json(geo["source"]).decode("utf-8")
        evidence_digest = geo["evidence_digest"]
    else:
        latitude = longitude = precision = method = None
        evidence_count = evidence_radius_km = geo_source = evidence_digest = None
    return (
        row["cep"],
        row["prefix"],
        row["street"],
        row["complement"],
        row["unit"],
        row["neighborhood"],
        row["city"],
        row["uf"],
        row["state"],
        row["region"],
        row["ibge"],
        latitude,
        longitude,
        precision,
        method,
        evidence_count,
        evidence_radius_km,
        geo_source,
        evidence_digest,
        row["dataset_version"],
    )


def _validate_identity(
    value: object, label: str, *, expected_format: str | None = None
) -> dict[str, object]:
    required = {"filename", "bytes", "sha256"}
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError(f"{label} has an invalid artifact identity")
    if (
        not isinstance(value.get("filename"), str)
        or Path(value["filename"]).name != value["filename"]
        or isinstance(value.get("bytes"), bool)
        or not isinstance(value.get("bytes"), int)
        or value["bytes"] < 1
        or not isinstance(value.get("sha256"), str)
        or _SHA256_RE.fullmatch(value["sha256"]) is None
        or (expected_format is not None and value.get("format") != expected_format)
    ):
        raise ValueError(f"{label} has an invalid artifact identity")
    return value


def _load_refresh_manifest(
    path: Path, normalized: Path, quality_path: Path, diff_path: Path
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    manifest_record = _regular_file_identity(path, "refresh manifest")
    if manifest_record["bytes"] > _MAX_REFRESH_MANIFEST_BYTES:
        raise ValueError("refresh manifest exceeds the size limit")
    raw_manifest = path.read_bytes()
    if (
        len(raw_manifest) != manifest_record["bytes"]
        or hashlib.sha256(raw_manifest).hexdigest() != manifest_record["sha256"]
    ):
        raise ValueError("refresh manifest changed while it was being read")
    try:
        document = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid refresh manifest: {exc}") from exc
    expected_keys = {
        "format",
        "status",
        "dataset_version",
        "inputs",
        "candidate_rows",
        "classification_counts",
        "artifacts",
        "inherited_base_release",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise ValueError("refresh manifest has invalid top-level keys")
    if document.get("format") != _REFRESH_MANIFEST_FORMAT:
        raise ValueError("unsupported refresh manifest format")
    if document.get("status") != _REFRESH_MANIFEST_STATUS:
        raise ValueError("refresh manifest has an invalid candidate status")
    dataset_version = document.get("dataset_version")
    if (
        not isinstance(dataset_version, str)
        or _VERSION_RE.fullmatch(dataset_version) is None
    ):
        raise ValueError("refresh manifest has an invalid dataset version")
    candidate_rows = document.get("candidate_rows")
    if (
        isinstance(candidate_rows, bool)
        or not isinstance(candidate_rows, int)
        or candidate_rows < 1
    ):
        raise ValueError("refresh manifest has an invalid candidate row count")
    inputs = document.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "current_opencepgeo",
        "current_release_contract",
        "correios_snapshot",
    }:
        raise ValueError("refresh manifest has invalid input provenance")
    current = inputs.get("current_opencepgeo")
    if (
        not isinstance(current, dict)
        or set(current)
        != {"filename", "dataset_version", "record_count", "bytes", "sha256"}
        or not isinstance(current.get("filename"), str)
        or Path(current["filename"]).name != current["filename"]
        or not isinstance(current.get("dataset_version"), str)
        or _VERSION_RE.fullmatch(current["dataset_version"]) is None
        or any(
            isinstance(current.get(key), bool)
            or not isinstance(current.get(key), int)
            or current[key] < 1
            for key in ("record_count", "bytes")
        )
        or not isinstance(current.get("sha256"), str)
        or _SHA256_RE.fullmatch(current["sha256"]) is None
    ):
        raise ValueError("refresh manifest has invalid current OpenCEPGeo provenance")
    current_contract = _validate_identity(
        inputs.get("current_release_contract"), "current release contract"
    )
    if set(current_contract) != {"filename", "bytes", "sha256"}:
        raise ValueError("current release contract has unexpected fields")
    correios = inputs.get("correios_snapshot")
    correios_keys = {
        "addresses_bytes",
        "addresses_sha256",
        "captured_at",
        "cep_type_counts",
        "date_only_expiry_semantics",
        "directory",
        "dnec_published_at",
        "dnec_timezone_semantics",
        "duplicate_record_count",
        "duplicate_group_count",
        "endpoint",
        "first_cep",
        "ibge_resolution_counts",
        "last_cep",
        "manifest_sha256",
        "page_count",
        "page_size",
        "raw_addresses_bytes",
        "raw_addresses_sha256",
        "raw_cep_type_counts",
        "raw_record_count",
        "raw_validity_counts",
        "record_count",
        "schema_version",
        "sort",
        "source",
        "source_total_elements",
        "validity_counts",
    }
    if (
        not isinstance(correios, dict)
        or set(correios) != correios_keys
        or not isinstance(correios.get("directory"), str)
        or Path(correios["directory"]).name != correios["directory"]
        or correios.get("schema_version") != 3
        or correios.get("source") != "correios-busca-cep-v3"
        or correios.get("endpoint") != "/cep/v2/enderecos"
        or correios.get("sort") != ["cep,asc"]
        or any(
            not isinstance(correios.get(key), str)
            or _SHA256_RE.fullmatch(correios[key]) is None
            for key in (
                "manifest_sha256",
                "addresses_sha256",
                "raw_addresses_sha256",
            )
        )
        or any(
            not isinstance(correios.get(key), str) or not correios[key]
            for key in (
                "dnec_published_at",
                "dnec_timezone_semantics",
                "captured_at",
                "date_only_expiry_semantics",
            )
        )
        or any(
            isinstance(correios.get(key), bool)
            or not isinstance(correios.get(key), int)
            or correios[key] < minimum
            for key, minimum in (
                ("addresses_bytes", 1),
                ("raw_addresses_bytes", 1),
                ("record_count", 1),
                ("raw_record_count", 1),
                ("source_total_elements", 1),
                ("duplicate_record_count", 0),
                ("duplicate_group_count", 0),
                ("page_count", 1),
                ("page_size", 1),
            )
        )
        or any(
            not isinstance(correios.get(key), str)
            or _CEP_RE.fullmatch(correios[key]) is None
            for key in ("first_cep", "last_cep")
        )
        or correios.get("first_cep") > correios.get("last_cep")
        or any(
            not isinstance(correios.get(key), dict)
            or set(correios[key]) != expected_keys
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in correios[key].values()
            )
            for key, expected_keys in (
                ("cep_type_counts", _CEP_TYPE_KEYS),
                ("raw_cep_type_counts", _CEP_TYPE_KEYS),
                ("validity_counts", _VALIDITY_KEYS),
                ("raw_validity_counts", _VALIDITY_KEYS),
                ("ibge_resolution_counts", _IBGE_RESOLUTION_KEYS),
            )
        )
    ):
        raise ValueError("refresh manifest has invalid Correios snapshot provenance")
    if (
        sum(correios["cep_type_counts"].values()) != correios["record_count"]
        or sum(correios["raw_cep_type_counts"].values()) != correios["raw_record_count"]
        or sum(correios["validity_counts"].values()) != correios["record_count"]
        or sum(correios["raw_validity_counts"].values()) != correios["raw_record_count"]
        or sum(correios["ibge_resolution_counts"].values()) != correios["record_count"]
        or correios["record_count"]
        != correios["raw_record_count"] - correios["duplicate_record_count"]
        or correios["duplicate_group_count"] > correios["duplicate_record_count"]
        or correios["source_total_elements"] != correios["raw_record_count"]
        or correios["page_count"]
        != (correios["raw_record_count"] + correios["page_size"] - 1)
        // correios["page_size"]
    ):
        raise ValueError("refresh manifest has inconsistent Correios counts")
    classifications = document.get("classification_counts")
    if (
        not isinstance(classifications, dict)
        or set(classifications) != _CLASSIFICATION_KEYS
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in classifications.values()
        )
        or sum(classifications.values()) != candidate_rows
        or candidate_rows != current["record_count"] + classifications["added"]
        or candidate_rows
        != correios["record_count"] + classifications["missing_from_source"]
        or current["record_count"]
        != sum(classifications[key] for key in _CLASSIFICATION_KEYS if key != "added")
        or correios["record_count"]
        != sum(
            classifications[key]
            for key in _CLASSIFICATION_KEYS
            if key != "missing_from_source"
        )
    ):
        raise ValueError("refresh manifest has invalid classification counts")
    inherited = document.get("inherited_base_release")
    inherited_keys = {
        "build_manifest",
        "builder",
        "contract",
        "dataset_version",
        "enrichment",
        "normalized_artifact",
        "publication_gate",
        "quality_pass_value",
        "quality_policy",
        "quality_report",
        "record_count",
        "release_manifest",
        "release_status",
        "source_lock",
    }
    if not isinstance(inherited, dict) or set(inherited) != inherited_keys:
        raise ValueError("refresh manifest has invalid inherited release lineage")
    for key, artifact_format in (
        ("build_manifest", _MANIFEST_FORMAT),
        ("contract", "px-opencepgeo-import-contract-v1"),
        ("enrichment", "opencepgeo-enrichment-v1"),
        ("normalized_artifact", _EXPORT_FORMAT),
        ("quality_policy", "opencepgeo-quality-policy-v2"),
        ("quality_report", "opencepgeo-quality-report-v2"),
        ("release_manifest", "opencepgeo-release-manifest-v2"),
        ("source_lock", "opencepgeo-source-lock-v1"),
    ):
        _validate_identity(
            inherited.get(key), f"inherited {key}", expected_format=artifact_format
        )
    builder = inherited.get("builder")
    if (
        not isinstance(builder, dict)
        or set(builder) != {"name", "version", "source_tree_sha256"}
        or builder.get("name") != "opencepgeo"
        or not isinstance(builder.get("version"), str)
        or not isinstance(builder.get("source_tree_sha256"), str)
        or _SHA256_RE.fullmatch(builder["source_tree_sha256"]) is None
        or not isinstance(inherited.get("dataset_version"), str)
        or inherited["dataset_version"] == dataset_version
        or inherited.get("record_count") != current["record_count"]
        or inherited["normalized_artifact"]["sha256"] != current["sha256"]
        or inherited["normalized_artifact"]["bytes"] != current["bytes"]
        or inherited["contract"]["sha256"] != current_contract["sha256"]
        or inherited["contract"]["bytes"] != current_contract["bytes"]
        or not isinstance(inherited.get("publication_gate"), str)
        or not inherited["publication_gate"]
        or not isinstance(inherited.get("quality_pass_value"), str)
        or not inherited["quality_pass_value"]
        or not isinstance(inherited.get("release_status"), str)
        or not inherited["release_status"]
    ):
        raise ValueError("refresh manifest inherited release identities disagree")
    expected_artifacts = {
        f"opencepgeo-{dataset_version}.jsonl": _EXPORT_FORMAT,
        f"opencepgeo-{dataset_version}.sqlite": (
            "opencepgeo-correios-candidate-sqlite-v1"
        ),
        "diff.jsonl": "opencepgeo-correios-refresh-diff-v1",
        "quality-report.json": "opencepgeo-correios-refresh-quality-v1",
    }
    artifacts = document.get("artifacts")
    if (
        normalized.name != f"opencepgeo-{dataset_version}.jsonl"
        or not isinstance(artifacts, dict)
        or set(artifacts) != set(expected_artifacts)
        or any(
            not isinstance(descriptor, dict)
            or set(descriptor) != {"format", "bytes", "sha256"}
            or descriptor.get("format") != expected_artifacts[name]
            or isinstance(descriptor.get("bytes"), bool)
            or not isinstance(descriptor.get("bytes"), int)
            or descriptor["bytes"] < 1
            or not isinstance(descriptor.get("sha256"), str)
            or _SHA256_RE.fullmatch(descriptor["sha256"]) is None
            for name, descriptor in artifacts.items()
        )
    ):
        raise ValueError("refresh manifest has an invalid artifact contract")
    artifact = artifacts[normalized.name]
    actual = _regular_file_identity(normalized, "normalized input")
    if actual["bytes"] != artifact["bytes"] or actual["sha256"] != artifact["sha256"]:
        raise ValueError("normalized input does not match refresh manifest")
    normalized_record = {**actual, "format": _EXPORT_FORMAT}
    quality_record = _regular_file_identity(quality_path, "refresh quality report")
    diff_record = _regular_file_identity(diff_path, "refresh diff")
    for candidate, expected, label in (
        (quality_record, artifacts[quality_path.name], "refresh quality report"),
        (diff_record, artifacts[diff_path.name], "refresh diff"),
    ):
        if (
            candidate["bytes"] != expected["bytes"]
            or candidate["sha256"] != expected["sha256"]
        ):
            raise ValueError(f"{label} does not match refresh manifest")
    try:
        quality = json.loads(quality_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid refresh quality report: {exc}") from exc
    expected_quality_keys = {
        "candidate_rows",
        "classification_counts",
        "correios_snapshot",
        "dataset_version",
        "format",
        "geography_action_counts",
        "inherited_base_release",
        "invariants",
        "located_rows",
        "precision_counts",
        "unresolved_rows",
    }
    precision_counts = (
        quality.get("precision_counts") if isinstance(quality, dict) else None
    )
    action_counts = (
        quality.get("geography_action_counts") if isinstance(quality, dict) else None
    )

    def valid_count_map(value: object, keys: frozenset[str]) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == keys
            and all(
                isinstance(count, int) and not isinstance(count, bool) and count >= 0
                for count in value.values()
            )
        )

    if (
        not isinstance(quality, dict)
        or set(quality) != expected_quality_keys
        or quality.get("format") != "opencepgeo-correios-refresh-quality-v1"
        or quality.get("dataset_version") != dataset_version
        or quality.get("candidate_rows") != candidate_rows
        or quality.get("classification_counts") != classifications
        or quality.get("correios_snapshot")
        != {
            key: value
            for key, value in correios.items()
            if key not in {"directory", "manifest_sha256", "addresses_sha256"}
        }
        or quality.get("inherited_base_release") != inherited
        or quality.get("invariants")
        != {
            "active_database_mutated": False,
            "candidate_rows_equal_located_plus_unresolved": True,
            "correios_snapshot_hash_verified": True,
            "current_input_stable_across_build": True,
            "current_rows_retained": True,
        }
        or quality.get("located_rows", 0) + quality.get("unresolved_rows", 0)
        != candidate_rows
        or not valid_count_map(precision_counts, _REFRESH_PRECISION_KEYS)
        or sum(precision_counts.values()) != quality.get("located_rows")
        or not valid_count_map(action_counts, _GEOGRAPHY_ACTION_KEYS)
        or sum(action_counts.values()) != candidate_rows
    ):
        raise ValueError("refresh quality report disagrees with refresh manifest")
    diff_classifications, diff_actions = _stream_refresh_diff(diff_path, diff_record)
    if diff_classifications != classifications or diff_actions != action_counts:
        raise ValueError("refresh diff counts disagree with refresh quality")
    quality_record["format"] = "opencepgeo-correios-refresh-quality-v1"
    diff_record["format"] = "opencepgeo-correios-refresh-diff-v1"
    return (
        document,
        manifest_record,
        normalized_record,
        quality_record,
        diff_record,
        quality,
    )


def _verify_bound_artifact(
    path: Path,
    expected: dict[str, object],
    label: str,
    *,
    require_filename: bool = True,
) -> dict[str, object]:
    actual = _regular_file_identity(path, label)
    identity_keys = (
        ("filename", "bytes", "sha256")
        if require_filename
        else (
            "bytes",
            "sha256",
        )
    )
    if any(actual[key] != expected[key] for key in identity_keys):
        raise ValueError(f"{label} does not match the inherited release identity")
    return actual


def _read_bound_json(
    path: Path,
    expected: dict[str, object],
    label: str,
    expected_format: str,
    *,
    require_filename: bool = True,
) -> dict[str, object]:
    identity = _verify_bound_artifact(
        path, expected, label, require_filename=require_filename
    )
    payload = path.read_bytes()
    if (
        len(payload) != identity["bytes"]
        or hashlib.sha256(payload).hexdigest() != identity["sha256"]
    ):
        raise ValueError(f"{label} changed while it was being read")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != expected_format:
        raise ValueError(f"{label} has an unexpected format")
    return document


def _validate_file_map(value: object, label: str) -> dict[str, dict[str, object]]:
    if not (
        isinstance(value, dict)
        and bool(value)
        and all(
            isinstance(name, str)
            and Path(name).name == name
            and isinstance(identity, dict)
            and set(identity) == {"bytes", "format", "sha256"}
            and isinstance(identity["bytes"], int)
            and not isinstance(identity["bytes"], bool)
            and identity["bytes"] > 0
            and isinstance(identity["format"], str)
            and bool(identity["format"])
            and isinstance(identity["sha256"], str)
            and _SHA256_RE.fullmatch(identity["sha256"]) is not None
            for name, identity in value.items()
        )
    ):
        raise ValueError(f"{label} has invalid file identities")
    return value


def _validate_current_release_contract(
    document: dict[str, object],
    inherited: dict[str, object],
    *,
    release_files: object,
    auxiliary_root: Path,
) -> None:
    expected_row_keys = [
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
        "dataset_version",
        "geo",
    ]
    expected_string_limits = {
        "dataset_version": 128,
        **_STRING_LIMITS,
        "method": 128,
    }
    expected_scalars = {
        "format": "px-opencepgeo-import-contract-v1",
        "manifest_format": _MANIFEST_FORMAT,
        "schema_version": _SCHEMA_VERSION,
        "artifact_format": _EXPORT_FORMAT,
        "evidence_radius_field": "evidence_radius_km",
        "normalized_artifact_path": ["artifacts", "normalized"],
        "statistics_path": ["statistics"],
        "sources_path": ["sources"],
        "source_lock_path": ["source_lock"],
        "builder_path": ["builder"],
        "quality_path": ["configuration", "quality"],
        "builder_required_keys": ["name", "version", "source_tree_sha256"],
        "quality_required_keys": ["version", "sha256"],
        "quality_status_field": "version",
        "quality_pass_value": inherited["quality_pass_value"],
        "coordinate_bounds": {
            "latitude_min": -34.0,
            "latitude_max": 5.5,
            "longitude_min": -74.0,
            "longitude_max": -28.0,
        },
        "require_city": True,
        "require_ibge": True,
        "source_category_pattern": "^[a-z0-9][a-z0-9_.:-]{0,63}$",
        "source_category_max_utf8_bytes": 64,
        "source_categories_sorted": True,
        "max_jsonl_line_bytes": _MAX_NORMALIZED_LINE_BYTES,
        "max_geo_source_count": 16,
        "max_geo_source_serialized_bytes": 2048,
        "row_keys": expected_row_keys,
        "string_byte_limits": expected_string_limits,
    }
    if set(document) != _PRICE_INDEX_CONTRACT_KEYS or any(
        document.get(key) != value for key, value in expected_scalars.items()
    ):
        raise ValueError("current release contract has an invalid import contract")
    approved = document.get("approved_release")
    approved_keys = {
        "dataset_version",
        "release_manifest_filename",
        "release_manifest_bytes",
        "release_manifest_format",
        "release_manifest_sha256",
        "release_status",
        "publication_gate",
        "record_count",
        "artifact_filename",
        "builder",
        "auxiliary_files",
        "files",
    }
    release_manifest = inherited["release_manifest"]
    normalized = inherited["normalized_artifact"]
    if (
        not isinstance(approved, dict)
        or set(approved) != approved_keys
        or approved.get("dataset_version") != inherited["dataset_version"]
        or approved.get("release_manifest_filename") != release_manifest["filename"]
        or approved.get("release_manifest_bytes") != release_manifest["bytes"]
        or approved.get("release_manifest_format") != release_manifest["format"]
        or approved.get("release_manifest_sha256") != release_manifest["sha256"]
        or approved.get("release_status") != inherited["release_status"]
        or approved.get("publication_gate") != inherited["publication_gate"]
        or approved.get("record_count") != inherited["record_count"]
        or approved.get("artifact_filename") != normalized["filename"]
        or approved.get("builder") != inherited["builder"]
    ):
        raise ValueError("current release contract disagrees with inherited release")

    files = _validate_file_map(
        approved.get("files"), "current release contract approved release"
    )
    auxiliary = _validate_file_map(
        approved.get("auxiliary_files"), "current release contract auxiliary"
    )
    validated_release_files = _validate_file_map(
        release_files, "inherited release manifest"
    )
    if files != validated_release_files:
        raise ValueError(
            "current release contract file map disagrees with inherited release"
        )
    for key in (
        "build_manifest",
        "enrichment",
        "normalized_artifact",
        "quality_policy",
        "quality_report",
        "source_lock",
    ):
        expected = inherited[key]
        if files.get(expected["filename"]) != {
            "bytes": expected["bytes"],
            "format": expected["format"],
            "sha256": expected["sha256"],
        }:
            raise ValueError(
                f"current release contract has invalid inherited {key} linkage"
            )
    for name, expected in auxiliary.items():
        actual = _regular_file_identity(auxiliary_root / name, f"inherited {name}")
        if any(actual[key] != expected[key] for key in ("bytes", "sha256")):
            raise ValueError(
                f"current release contract auxiliary file disagrees for {name}"
            )


def _verify_inherited_release(
    *,
    refresh: dict[str, object],
    inherited_release: Path,
    current_release_contract: Path,
    source_lock_path: Path,
    ibge_path: Path,
    osm_observations_path: Path,
    municipality_boundaries_path: Path,
    enrichment_config_path: Path,
    quality_config_path: Path,
) -> tuple[
    SourceLock,
    dict[str, object],
    list[dict[str, object]],
    dict[str, object],
    dict[str, object],
]:
    inherited = refresh["inherited_base_release"]
    if not isinstance(inherited, dict):
        raise ValueError("refresh manifest has invalid inherited release lineage")
    if not inherited_release.is_dir() or inherited_release.is_symlink():
        raise ValueError("inherited release must be a real directory")
    contract = refresh["inputs"]["current_release_contract"]
    contract_document = _read_bound_json(
        current_release_contract,
        contract,
        "current release contract",
        "px-opencepgeo-import-contract-v1",
    )
    inherited_documents: dict[str, dict[str, object]] = {}
    for key, expected_format in (
        ("build_manifest", _MANIFEST_FORMAT),
        ("normalized_artifact", _EXPORT_FORMAT),
        ("enrichment", "opencepgeo-enrichment-v1"),
        ("quality_policy", "opencepgeo-quality-policy-v2"),
        ("quality_report", "opencepgeo-quality-report-v2"),
        ("release_manifest", "opencepgeo-release-manifest-v2"),
        ("source_lock", "opencepgeo-source-lock-v1"),
    ):
        expected = inherited[key]
        candidate = inherited_release / str(expected["filename"])
        if key == "normalized_artifact":
            _verify_bound_artifact(candidate, expected, "inherited normalized artifact")
        else:
            inherited_documents[key] = _read_bound_json(
                candidate, expected, f"inherited {key}", expected_format
            )

    build_document = inherited_documents["build_manifest"]
    release_document = inherited_documents["release_manifest"]
    inherited_quality = inherited_documents["quality_report"]
    _validate_current_release_contract(
        contract_document,
        inherited,
        release_files=release_document.get("files"),
        auxiliary_root=inherited_release,
    )
    if (
        build_document.get("dataset_version") != inherited["dataset_version"]
        or build_document.get("builder") != inherited["builder"]
        or release_document.get("dataset_version") != inherited["dataset_version"]
        or release_document.get("release_status") != inherited["release_status"]
        or release_document.get("publication_gate") != inherited["publication_gate"]
        or release_document.get("builder") != inherited["builder"]
        or release_document.get("source_lock_sha256")
        != inherited["source_lock"]["sha256"]
        or inherited_quality.get("status") != "pass"
        or inherited_quality.get("dataset_version") != inherited["dataset_version"]
        or inherited_quality.get("quality_version") != inherited["quality_pass_value"]
        or not isinstance(inherited_quality.get("artifact"), dict)
        or inherited_quality["artifact"].get("record_count")
        != inherited["record_count"]
    ):
        raise ValueError("inherited release manifests disagree with refresh lineage")
    build_artifacts = build_document.get("artifacts")
    build_normalized = (
        build_artifacts.get("normalized") if isinstance(build_artifacts, dict) else None
    )
    current = refresh["inputs"]["current_opencepgeo"]
    if (
        not isinstance(build_normalized, dict)
        or any(build_normalized.get(key) != current[key] for key in ("bytes", "sha256"))
        or build_normalized.get("format") != inherited["normalized_artifact"]["format"]
        or current["dataset_version"] != inherited["dataset_version"]
        or current["record_count"] != inherited["record_count"]
    ):
        raise ValueError("inherited normalized artifact linkage is inconsistent")
    release_files = release_document.get("files")
    if not isinstance(release_files, dict):
        raise ValueError("inherited release manifest has no file identities")
    for key in (
        "build_manifest",
        "normalized_artifact",
        "enrichment",
        "quality_policy",
        "quality_report",
        "source_lock",
    ):
        expected = inherited[key]
        release_record = release_files.get(expected["filename"])
        if release_record != {
            "bytes": expected["bytes"],
            "sha256": expected["sha256"],
            "format": expected["format"],
        }:
            raise ValueError(f"inherited release file linkage disagrees for {key}")
    attestation = release_document.get("quality_attestation")
    if (
        not isinstance(attestation, dict)
        or attestation.get("report_sha256") != inherited["quality_report"]["sha256"]
        or attestation.get("build_manifest_sha256")
        != inherited["build_manifest"]["sha256"]
    ):
        raise ValueError("inherited quality attestation linkage is inconsistent")

    lock_expected = inherited["source_lock"]
    lock = load_source_lock(source_lock_path)
    if lock.release != inherited["dataset_version"]:
        raise ValueError("source lock release does not match inherited RC2 lineage")
    if lock.release == refresh["dataset_version"]:
        raise ValueError(
            "candidate dataset must not masquerade as the source-lock release"
        )
    lock_document = _read_bound_json(
        source_lock_path,
        lock_expected,
        "source lock",
        "opencepgeo-source-lock-v1",
        require_filename=False,
    )
    lock_metadata = {
        "filename": source_lock_path.name,
        "bytes": lock_expected["bytes"],
        "sha256": lock_expected["sha256"],
        "publication_gate": lock_document.get("publication_gate"),
        "release": lock.release,
    }

    ibge_source = _locked_source(lock, ibge_path)
    boundary_source = _locked_source(lock, municipality_boundaries_path)
    if ibge_source.source_id == boundary_source.source_id:
        raise ValueError(
            "IBGE and municipality boundaries use the same source-lock entry"
        )
    raw_members = boundary_source.metadata.get("members")
    if not isinstance(raw_members, dict) or not raw_members:
        raise ValueError("locked municipality boundaries require member identities")

    ibge_record = _input_record(ibge_path)
    boundary_record = _input_record(municipality_boundaries_path)
    boundary_record["members"] = raw_members
    for record, source, label in (
        (ibge_record, ibge_source, "IBGE"),
        (boundary_record, boundary_source, "municipality boundaries"),
    ):
        if record["bytes"] != source.byte_size or record["sha256"] != source.sha256:
            raise ValueError(f"{label} changed while it was being verified")

    enrichment_expected = inherited["enrichment"]
    quality_expected = inherited["quality_policy"]
    for candidate, expected, label in (
        (enrichment_config_path, enrichment_expected, "enrichment config"),
        (quality_config_path, quality_expected, "quality config"),
    ):
        actual = _regular_file_identity(candidate, label)
        if (
            actual["bytes"] != expected["bytes"]
            or actual["sha256"] != expected["sha256"]
        ):
            raise ValueError(f"{label} does not match inherited RC2 lineage")

    osm_record = _osm_observations_metadata(osm_observations_path, lock_metadata)
    inherited_osm = build_document.get("configuration", {}).get("osm_observations")
    if osm_record != inherited_osm:
        raise ValueError("OSM evidence does not match inherited RC2 build lineage")
    inherited_inputs = build_document.get("inputs", {})
    inherited_configuration = build_document.get("configuration", {})
    inherited_sources = build_document.get("sources")
    lock_source_ids = {source.source_id for source in lock.sources}
    if (
        not isinstance(inherited_inputs, dict)
        or not isinstance(inherited_configuration, dict)
        or not isinstance(inherited_sources, list)
        or not inherited_sources
        or any(
            not isinstance(source, dict)
            or not isinstance(source.get("id"), str)
            or source["id"] not in lock_source_ids
            for source in inherited_sources
        )
        or len({source["id"] for source in inherited_sources}) != len(inherited_sources)
        or inherited_inputs.get("ibge") != ibge_record
        or inherited_inputs.get("municipality_boundaries") != boundary_record
        or build_document.get("source_lock", {}).get("sha256")
        != lock_metadata["sha256"]
        or build_document.get("configuration", {}).get("enrichment", {}).get("sha256")
        != enrichment_expected["sha256"]
        or build_document.get("configuration", {}).get("quality", {}).get("sha256")
        != quality_expected["sha256"]
    ):
        raise ValueError("supplied inputs do not reproduce inherited RC2 build lineage")
    return (
        lock,
        dict(build_document["source_lock"]),
        inherited_sources,
        inherited_inputs,
        inherited_configuration,
    )


def _fill_normalized_geo(
    row: dict[str, object],
    municipalities: dict[str, object],
    administrative: dict[str, object],
    counters: dict[str, int],
) -> dict[str, object]:
    if row["geo"] is not None:
        counters["geo_inherited"] += 1
        return row
    ibge = str(row["ibge"])
    reference = municipalities.get(ibge)
    method = "ibge_municipality_reference"
    if reference is None:
        reference = administrative.get(ibge)
        method = "ibge_administrative_locality_aggregate"
    if reference is None:
        counters["geo_unresolved"] += 1
        return row
    counter = (
        "geo_filled_municipality"
        if method == "ibge_municipality_reference"
        else "geo_filled_administrative"
    )
    counters[counter] += 1
    return {
        **row,
        "geo": {
            "type": "Point",
            "coordinates": [reference.point.longitude, reference.point.latitude],
            "precision": "municipality",
            "method": method,
            "evidence_count": reference.evidence_count,
            "evidence_radius_km": reference.evidence_radius_km,
            "source": [reference.point.source],
            "evidence_digest": reference.evidence_digest,
        },
    }


def _percentile_km(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _displacement_summary(values: list[float]) -> dict[str, object]:
    if not values:
        return {"count": 0, "max_km": 0.0, "mean_km": 0.0, "p50_km": 0.0, "p95_km": 0.0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "max_km": round(ordered[-1], 6),
        "mean_km": round(math.fsum(ordered) / len(ordered), 6),
        "p50_km": round(_percentile_km(ordered, 0.50), 6),
        "p95_km": round(_percentile_km(ordered, 0.95), 6),
    }


def _classify_osm_polygon(
    targets: list[tuple[str, str, float, float]],
    *,
    municipality_boundaries_path: Path,
    boundary_members: dict[str, object] | None,
    max_outside_fraction: float,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "checked": len(targets),
        "interior": 0,
        "boundary": 0,
        "outside": 0,
        "unknown": 0,
        "outside_fraction": 0.0,
        "maximum_outside_fraction": max_outside_fraction,
    }
    if not targets:
        return summary
    observations = [
        Observation(
            cep=cep,
            point=Point(latitude, longitude, f"coordinate-validation:node/{index}"),
        )
        for index, (cep, _ibge, longitude, latitude) in enumerate(targets)
    ]
    selection = select_municipality_observations(
        municipality_boundaries_path,
        observations,
        {cep: ibge for cep, ibge, _lon, _lat in targets},
        expected_members=boundary_members,
    )
    summary["interior"] = len(selection.interior_target_municipality)
    summary["boundary"] = len(selection.boundary_target_municipality)
    summary["outside"] = len(selection.outside_target_municipality)
    summary["unknown"] = len(selection.unknown_cep)
    known = int(summary["checked"]) - int(summary["unknown"])
    summary["outside_fraction"] = (
        round(int(summary["outside"]) / known, 8) if known else 0.0
    )
    if summary["outside_fraction"] > max_outside_fraction:
        raise ValueError(
            "osm_postcode coordinates fall outside their municipality polygon: "
            f"{summary['outside']}/{known} (fraction {summary['outside_fraction']} "
            f"exceeds the permitted {max_outside_fraction})"
        )
    return summary


def _coordinate_evidence_estimator(
    municipalities: dict[str, object],
    osm_observations_path: Path,
    enrichment: EnrichmentConfig,
) -> CentroidEstimator:
    # Mirrors the production build's estimator (no first-party observations); its
    # osm_postcode / municipality tiers deterministically reproduce the candidate
    # coordinates from the same pinned inputs that built the inherited release.
    return CentroidEstimator(
        (),
        municipalities,
        osm_observations=load_osm_observations(osm_observations_path),
        min_prefix_samples=enrichment.min_prefix_samples,
        max_prefix_radius_km=enrichment.max_prefix_radius_km,
        max_observed_radius_km=enrichment.max_observed_radius_km,
        max_osm_radius_km=enrichment.max_osm_radius_km,
        max_osm_municipality_distance_km=enrichment.max_osm_municipality_distance_km,
        outlier_min_samples=enrichment.outlier_min_samples,
        outlier_mad_multiplier=enrichment.outlier_mad_multiplier,
        outlier_floor_km=enrichment.outlier_floor_km,
    )


def _validate_candidate_geography_evidence(
    *,
    candidate_path: Path,
    candidate_record: dict[str, object],
    candidate_dataset_version: str,
    inherited_path: Path,
    inherited_record: dict[str, object],
    inherited_dataset_version: str,
    osm_observations_path: Path,
    municipality_boundaries_path: Path,
    boundary_members: dict[str, object] | None,
    municipalities: dict[str, object],
    administrative: dict[str, object],
    enrichment: EnrichmentConfig,
    max_outside_polygon_fraction: float,
) -> dict[str, object]:
    """Prove every non-null candidate coordinate against the pinned evidence.

    Streams the inherited release alongside the candidate (both CEP-ordered) and
    accepts a coordinate only when it is byte-identical to the inherited release
    (already proven when that release built) or reproduced from the pinned
    IBGE/OSM inputs for its declared precision tier: a ``municipality`` point
    must sit exactly on the pinned IBGE city/administrative reference for its
    IBGE code, and an ``osm_postcode`` (or first-party) point must equal the
    production estimator's recomputed point for the same tier. Any coordinate
    that is neither preserved nor reproduced has been moved off its evidence and
    fails the build. osm_postcode points are additionally checked for
    municipality polygon containment, gated on the fraction the quality policy
    tolerates. Returns a displacement/suspicious-change summary for the manifest.
    """
    estimator: CentroidEstimator | None = None
    inherited_rows = _iter_normalized_rows(
        inherited_path, inherited_dataset_version, inherited_record
    )
    inherited_current = next(inherited_rows, None)

    non_null = 0
    preserved = 0
    reproduced = 0
    reproduced_new = 0
    reproduced_changed = 0
    displacements: list[float] = []
    suspicious: list[str] = []
    osm_targets: list[tuple[str, str, float, float]] = []

    for row in _iter_normalized_rows(
        candidate_path, candidate_dataset_version, candidate_record
    ):
        cep = str(row["cep"])
        while inherited_current is not None and str(inherited_current["cep"]) < cep:
            inherited_current = next(inherited_rows, None)
        inherited_geo = None
        if inherited_current is not None and str(inherited_current["cep"]) == cep:
            inherited_geo = inherited_current["geo"]

        geo = row["geo"]
        if geo is None:
            # Null candidate geography is filled deterministically from the pinned
            # IBGE reference by _fill_normalized_geo; evidence-derived by
            # construction, so it needs no separate proof here.
            continue
        non_null += 1
        if inherited_geo is not None and geo == inherited_geo:
            preserved += 1
            continue

        ibge = str(row["ibge"])
        precision = geo.get("precision")
        coordinates = geo["coordinates"]
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
        if precision == "municipality":
            # A municipality point is a copy of the pinned IBGE city or
            # administrative-locality reference; it must sit exactly on that
            # reference for its IBGE code, independent of any recompute tier.
            reference = municipalities.get(ibge) or administrative.get(ibge)
            reproduced_ok = reference is not None and (
                reference.point.longitude == longitude
                and reference.point.latitude == latitude
            )
        elif precision in ("osm_postcode", "observed_cep", "observed_cep_prefix"):
            # Higher-precision points are recomputed by the production estimator;
            # the recomputed tier and point must match the stored coordinate.
            if estimator is None:
                estimator = _coordinate_evidence_estimator(
                    municipalities, osm_observations_path, enrichment
                )
            estimate = estimator.estimate(cep, ibge)
            reproduced_ok = (
                estimate is not None
                and estimate.precision == precision
                and estimate.longitude == longitude
                and estimate.latitude == latitude
            )
        else:
            reproduced_ok = False
        if not reproduced_ok:
            suspicious.append(f"{cep} [{precision}]")
            continue
        reproduced += 1
        if inherited_geo is None:
            reproduced_new += 1
        else:
            reproduced_changed += 1
            previous = inherited_geo["coordinates"]
            displacements.append(
                haversine_km(
                    Point(float(previous[1]), float(previous[0]), "inherited"),
                    Point(latitude, longitude, "candidate"),
                )
            )
        if precision == "osm_postcode":
            osm_targets.append((cep, ibge, longitude, latitude))

    if suspicious:
        preview = ", ".join(suspicious[:5])
        raise ValueError(
            f"{len(suspicious)} candidate coordinate(s) are neither preserved from "
            "the inherited release nor reproduced from the pinned IBGE/OSM "
            f"evidence: {preview}"
        )

    polygon = _classify_osm_polygon(
        osm_targets,
        municipality_boundaries_path=municipality_boundaries_path,
        boundary_members=boundary_members,
        max_outside_fraction=max_outside_polygon_fraction,
    )
    return {
        "policy": _COORDINATE_EVIDENCE_POLICY,
        "non_null_candidate_rows": non_null,
        "preserved_from_inherited": preserved,
        "reproduced_from_pinned_evidence": reproduced,
        "reproduced_new_cep": reproduced_new,
        "reproduced_changed_cep": reproduced_changed,
        "suspicious_changes": len(suspicious),
        "displacement_km": _displacement_summary(displacements),
        "osm_polygon": polygon,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_artifact_group(
    staged_targets: list[tuple[Path, Path]],
    *,
    force: bool,
    parent_descriptor: int,
    parent_path: Path,
) -> None:
    anchored = os.fstat(parent_descriptor)
    anchored_identity = (anchored.st_dev, anchored.st_ino)
    lock_descriptor = os.open(
        ".opencepgeo-publish.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_descriptor,
    )
    if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
        os.close(lock_descriptor)
        raise ValueError("artifact publication lock must be a regular file")
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    backups: list[tuple[str, str]] = []
    published: list[tuple[Path, str]] = []
    recovery_name: str | None = None
    recovery_descriptor: int | None = None

    def entry_stat(name: str, directory_descriptor: int) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def staged_is_published(staged: Path, target_name: str) -> bool:
        target_stat = entry_stat(target_name, parent_descriptor)
        if target_stat is None:
            return False
        staged_stat = staged.stat()
        return (target_stat.st_dev, target_stat.st_ino) == (
            staged_stat.st_dev,
            staged_stat.st_ino,
        )

    def validate_lexical_parents() -> None:
        for _, target in staged_targets:
            try:
                current_parent = os.stat(target.parent, follow_symlinks=True)
            except OSError as exc:
                raise RuntimeError(
                    f"build output directory changed before publication: {target.parent}"
                ) from exc
            if (current_parent.st_dev, current_parent.st_ino) != anchored_identity:
                raise RuntimeError(
                    f"build output directory changed before publication: {target.parent}"
                )

    try:
        validate_lexical_parents()
        if force:
            while recovery_name is None:
                candidate = f".opencepgeo-backup-{secrets.token_hex(8)}"
                try:
                    os.mkdir(candidate, 0o700, dir_fd=parent_descriptor)
                except FileExistsError:
                    continue
                recovery_name = candidate
            recovery_descriptor = os.open(
                recovery_name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
        for index, (staged, target) in enumerate(staged_targets):
            target_name = target.name
            if force:
                assert recovery_descriptor is not None
                backup_name = f"backup-{index}-{target_name}"
                while True:
                    target_stat = entry_stat(target_name, parent_descriptor)
                    if target_stat is None:
                        break
                    if not stat.S_ISREG(target_stat.st_mode):
                        raise ValueError(
                            f"build target must be a regular file: {target}"
                        )
                    try:
                        os.replace(
                            target_name,
                            backup_name,
                            src_dir_fd=parent_descriptor,
                            dst_dir_fd=recovery_descriptor,
                        )
                    except FileNotFoundError:
                        continue
                    backups.append((backup_name, target_name))
                    break
            os.link(
                staged,
                target_name,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            published.append((staged, target_name))
        if recovery_descriptor is not None:
            os.fsync(recovery_descriptor)
        os.fsync(parent_descriptor)
        validate_lexical_parents()
    except Exception as publication_error:
        rollback_errors: list[str] = []
        for staged, target_name in reversed(published):
            try:
                target_stat = entry_stat(target_name, parent_descriptor)
                if target_stat is None:
                    continue
                if staged_is_published(staged, target_name):
                    os.unlink(target_name, dir_fd=parent_descriptor)
                else:
                    rollback_errors.append(
                        f"preserve concurrently replaced target {parent_path / target_name}"
                    )
            except OSError as exc:
                rollback_errors.append(f"remove {parent_path / target_name}: {exc}")
        for backup_name, target_name in reversed(backups):
            try:
                if entry_stat(target_name, parent_descriptor) is not None:
                    rollback_errors.append(
                        f"preserve concurrent target {parent_path / target_name}; "
                        f"backup remains {recovery_name}/{backup_name}"
                    )
                    continue
                assert recovery_descriptor is not None
                os.replace(
                    backup_name,
                    target_name,
                    src_dir_fd=recovery_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            except OSError as exc:
                rollback_errors.append(
                    f"restore {parent_path / target_name} from "
                    f"{recovery_name}/{backup_name}: {exc}"
                )
        try:
            if recovery_descriptor is not None:
                os.fsync(recovery_descriptor)
            os.fsync(parent_descriptor)
        except OSError as exc:
            rollback_errors.append(f"fsync {parent_path}: {exc}")
        if rollback_errors:
            recovery = (
                str(parent_path / recovery_name)
                if recovery_name is not None
                else "no backup directory (new targets only)"
            )
            raise RuntimeError(
                "artifact publication failed and rollback was incomplete; "
                f"recover preserved backups from {recovery}; "
                + "; ".join(rollback_errors)
            ) from publication_error
        if recovery_descriptor is not None:
            os.close(recovery_descriptor)
            recovery_descriptor = None
        if recovery_name is not None:
            os.rmdir(recovery_name, dir_fd=parent_descriptor)
        raise
    else:
        if recovery_descriptor is not None:
            for backup_name, _ in backups:
                os.unlink(backup_name, dir_fd=recovery_descriptor)
            os.fsync(recovery_descriptor)
            os.close(recovery_descriptor)
            recovery_descriptor = None
        if recovery_name is not None:
            os.rmdir(recovery_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if recovery_descriptor is not None:
            os.close(recovery_descriptor)
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


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


def build_database_from_normalized(
    *,
    normalized_path: str | Path,
    normalized_output_path: str | Path,
    refresh_manifest_path: str | Path,
    refresh_quality_path: str | Path,
    refresh_diff_path: str | Path,
    inherited_release_path: str | Path,
    current_release_contract_path: str | Path,
    source_lock_path: str | Path,
    ibge_path: str | Path,
    osm_observations_path: str | Path,
    municipality_boundaries_path: str | Path,
    enrichment_config_path: str | Path,
    quality_config_path: str | Path,
    output_path: str | Path,
    manifest_path: str | Path,
    force: bool = False,
) -> dict[str, object]:
    normalized = Path(normalized_path)
    normalized_output = Path(normalized_output_path)
    refresh_manifest = Path(refresh_manifest_path)
    refresh_quality = Path(refresh_quality_path)
    refresh_diff = Path(refresh_diff_path)
    inherited_release = Path(inherited_release_path)
    current_release_contract = Path(current_release_contract_path)
    lock_path = Path(source_lock_path)
    ibge = Path(ibge_path)
    osm_observations = Path(osm_observations_path)
    osm_manifest = osm_observations.with_suffix(".manifest.json")
    municipality_boundaries = Path(municipality_boundaries_path)
    enrichment_config = Path(enrichment_config_path)
    quality_config = Path(quality_config_path)
    output = Path(output_path)
    manifest = Path(manifest_path)
    targets = (output, normalized_output, manifest)

    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    if len({target.parent.resolve() for target in targets}) != 1:
        raise ValueError(
            "SQLite, normalized output, and manifest require one directory"
        )
    for target in targets:
        if os.path.lexists(target):
            if target.is_symlink() or not target.is_file():
                raise ValueError(f"build target must be a regular file: {target}")
            if not force:
                raise FileExistsError(
                    f"output exists; pass --force to replace it: {target}"
                )

    inputs = [
        normalized,
        refresh_manifest,
        refresh_quality,
        refresh_diff,
        current_release_contract,
        lock_path,
        ibge,
        osm_observations,
        osm_manifest,
        municipality_boundaries,
        enrichment_config,
        quality_config,
    ]
    if inherited_release.is_dir():
        inputs.extend(path for path in inherited_release.iterdir() if path.is_file())

    def paths_collide(left: Path, right: Path) -> bool:
        if left.resolve() == right.resolve():
            return True
        if os.path.lexists(left) and os.path.lexists(right):
            try:
                return os.path.samefile(left, right)
            except OSError:
                return False
        return False

    for index, target in enumerate(targets):
        if any(paths_collide(target, other) for other in targets[index + 1 :]):
            raise ValueError("build output paths must be distinct")
        if any(paths_collide(target, source) for source in inputs):
            raise ValueError(f"build output collides with an input: {target}")

    output_parent = output.parent.resolve(strict=True)
    parent_descriptor = os.open(
        output_parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        staging_directory = Path(
            tempfile.mkdtemp(prefix=".opencepgeo-normalized-build-", dir=output_parent)
        )
        os.chmod(staging_directory, 0o700)
    except Exception:
        os.close(parent_descriptor)
        raise
    temporary_output = staging_directory / "database.sqlite"
    temporary_normalized = staging_directory / "normalized.jsonl"
    temporary_manifest = staging_directory / "build-manifest.json"
    snapshots = staging_directory / "inputs"
    try:
        refresh_snapshots = snapshots / "refresh"
        _snapshot_regular_file(
            normalized,
            refresh_snapshots / normalized.name,
            "normalized candidate",
        )
        _snapshot_regular_file(
            refresh_manifest,
            refresh_snapshots / refresh_manifest.name,
            "refresh manifest",
        )
        _snapshot_regular_file(
            refresh_quality,
            refresh_snapshots / refresh_quality.name,
            "refresh quality report",
        )
        _snapshot_regular_file(
            refresh_diff,
            refresh_snapshots / refresh_diff.name,
            "refresh diff",
        )
        normalized = refresh_snapshots / normalized.name
        refresh_manifest = refresh_snapshots / refresh_manifest.name
        refresh_quality = refresh_snapshots / refresh_quality.name
        refresh_diff = refresh_snapshots / refresh_diff.name
        (
            refresh,
            refresh_record,
            candidate_record,
            refresh_quality_record,
            refresh_diff_record,
            refresh_quality_document,
        ) = _load_refresh_manifest(
            refresh_manifest, normalized, refresh_quality, refresh_diff
        )

        pinned_snapshots = snapshots / "pinned"
        for source, label in (
            (lock_path, "source lock"),
            (ibge, "IBGE reference"),
            (osm_observations, "OSM observations"),
            (osm_manifest, "OSM observations manifest"),
            (municipality_boundaries, "municipality boundaries"),
            (enrichment_config, "enrichment config"),
            (quality_config, "quality config"),
        ):
            _snapshot_regular_file(source, pinned_snapshots / source.name, label)
        contract_snapshot = snapshots / "contract" / current_release_contract.name
        _snapshot_regular_file(
            current_release_contract,
            contract_snapshot,
            "current release contract",
        )
        contract_document = _read_bound_json(
            contract_snapshot,
            refresh["inputs"]["current_release_contract"],
            "current release contract",
            "px-opencepgeo-import-contract-v1",
        )
        approved_release = contract_document.get("approved_release")
        if not isinstance(approved_release, dict):
            raise ValueError("current release contract has no approved release")
        approved_files = _validate_file_map(
            approved_release.get("files"),
            "current release contract approved release",
        )
        auxiliary_files = _validate_file_map(
            approved_release.get("auxiliary_files"),
            "current release contract auxiliary",
        )
        inherited_snapshot = snapshots / "inherited-release"
        inherited_snapshot.mkdir()
        release_manifest_expected = refresh["inherited_base_release"][
            "release_manifest"
        ]
        release_manifest_name = str(release_manifest_expected["filename"])
        _snapshot_regular_file(
            inherited_release / release_manifest_name,
            inherited_snapshot / release_manifest_name,
            "inherited release manifest",
        )
        release_document = _read_bound_json(
            inherited_snapshot / release_manifest_name,
            release_manifest_expected,
            "inherited release manifest",
            "opencepgeo-release-manifest-v2",
        )
        release_files = _validate_file_map(
            release_document.get("files"), "inherited release manifest"
        )
        if approved_files != release_files:
            raise ValueError(
                "current release contract file map disagrees with inherited release"
            )
        if (
            release_manifest_name in approved_files
            or release_manifest_name in auxiliary_files
            or set(approved_files) & set(auxiliary_files)
        ):
            raise ValueError("inherited release file namespaces overlap")
        for name, expected in approved_files.items():
            actual = _snapshot_regular_file(
                inherited_release / name,
                inherited_snapshot / name,
                f"inherited release file {name}",
            )
            if any(actual[key] != expected[key] for key in ("bytes", "sha256")):
                raise ValueError(
                    f"current release contract approved file disagrees for {name}"
                )
        for name, expected in auxiliary_files.items():
            actual = _snapshot_regular_file(
                inherited_release / name,
                inherited_snapshot / name,
                f"inherited {name}",
            )
            if any(actual[key] != expected[key] for key in ("bytes", "sha256")):
                raise ValueError(
                    f"current release contract auxiliary file disagrees for {name}"
                )

        lock_path = pinned_snapshots / lock_path.name
        ibge = pinned_snapshots / ibge.name
        osm_observations = pinned_snapshots / osm_observations.name
        municipality_boundaries = pinned_snapshots / municipality_boundaries.name
        enrichment_config = pinned_snapshots / enrichment_config.name
        quality_config = pinned_snapshots / quality_config.name
        current_release_contract = contract_snapshot
        inherited_release = inherited_snapshot

        dataset_version = str(refresh["dataset_version"])
        (
            lock,
            lock_metadata,
            locked_sources,
            inherited_inputs,
            inherited_configuration,
        ) = _verify_inherited_release(
            refresh=refresh,
            inherited_release=inherited_release,
            current_release_contract=current_release_contract,
            source_lock_path=lock_path,
            ibge_path=ibge,
            osm_observations_path=osm_observations,
            municipality_boundaries_path=municipality_boundaries,
            enrichment_config_path=enrichment_config,
            quality_config_path=quality_config,
        )
        enrichment, enrichment_record = load_enrichment_config(enrichment_config)
        quality_policy = load_quality_policy(quality_config)

        candidate_rows = 0
        candidate_located = 0
        candidate_precision: Counter[str] = Counter()
        unresolved_ibge: set[str] = set()
        for candidate_rows, row in enumerate(
            _iter_normalized_rows(normalized, dataset_version, candidate_record),
            start=1,
        ):
            geo = row["geo"]
            if geo is None:
                unresolved_ibge.add(str(row["ibge"]))
            else:
                candidate_located += 1
                candidate_precision[str(geo["precision"])] += 1
        streamed_precision = {
            key: candidate_precision[key] for key in _REFRESH_PRECISION_KEYS
        }
        if (
            candidate_rows != refresh["candidate_rows"]
            or candidate_located != refresh_quality_document["located_rows"]
            or candidate_rows - candidate_located
            != refresh_quality_document["unresolved_rows"]
            or set(candidate_precision) - _REFRESH_PRECISION_KEYS
            or streamed_precision != refresh_quality_document["precision_counts"]
        ):
            raise ValueError("candidate geography counts disagree with refresh quality")

        municipalities = load_ibge_municipality_references(ibge)
        administrative = load_ibge_administrative_locality_references(
            ibge, unresolved_ibge - set(municipalities)
        )
        verify_file(ibge, _locked_source(lock, ibge))

        inherited_base = refresh["inherited_base_release"]
        inherited_normalized = inherited_release / str(
            inherited_base["normalized_artifact"]["filename"]
        )
        osm_policy = quality_policy.validation.get("osm_evidence", {})
        max_outside_fraction = osm_policy.get(
            "maximum_outside_target_municipality_fraction", 0.0
        )
        coordinate_validation = _validate_candidate_geography_evidence(
            candidate_path=normalized,
            candidate_record=candidate_record,
            candidate_dataset_version=dataset_version,
            inherited_path=inherited_normalized,
            inherited_record=inherited_base["normalized_artifact"],
            inherited_dataset_version=str(inherited_base["dataset_version"]),
            osm_observations_path=osm_observations,
            municipality_boundaries_path=municipality_boundaries,
            boundary_members=_locked_source(lock, municipality_boundaries).metadata.get(
                "members"
            ),
            municipalities=municipalities,
            administrative=administrative,
            enrichment=enrichment,
            max_outside_polygon_fraction=(
                float(max_outside_fraction)
                if isinstance(max_outside_fraction, (int, float))
                else 0.0
            ),
        )
    except Exception:
        shutil.rmtree(staging_directory, ignore_errors=True)
        os.close(parent_descriptor)
        raise

    identity = builder_identity()
    counters = {
        "geo_inherited": 0,
        "geo_filled_municipality": 0,
        "geo_filled_administrative": 0,
        "geo_unresolved": 0,
    }
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary_output)
        connection.executescript(_SCHEMA)
        metadata = [
            ("format", _SCHEMA_VERSION),
            ("dataset_version", dataset_version),
            ("builder_name", identity["name"]),
            ("builder_version", identity["version"]),
            ("builder_source_tree_sha256", identity["source_tree_sha256"]),
            ("enrichment_version", enrichment.version),
            ("min_prefix_samples", str(enrichment.min_prefix_samples)),
            ("max_prefix_radius_km", str(enrichment.max_prefix_radius_km)),
            ("normalized_refresh_candidate_sha256", str(candidate_record["sha256"])),
            ("normalized_refresh_manifest_sha256", str(refresh_record["sha256"])),
            (
                "normalized_refresh_quality_sha256",
                str(refresh_quality_record["sha256"]),
            ),
            ("normalized_refresh_diff_sha256", str(refresh_diff_record["sha256"])),
            ("source_lock_sha256", str(lock_metadata["sha256"])),
            ("enrichment_config_sha256", str(enrichment_record["sha256"])),
            ("quality_config_sha256", quality_policy.sha256),
            ("quality_version", quality_policy.version),
        ]
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)", metadata
        )
        normalized_digest = hashlib.sha256()
        normalized_bytes = 0

        def rows_for_insert() -> Iterator[tuple[object, ...]]:
            nonlocal normalized_bytes
            with temporary_normalized.open("xb") as output_handle:
                for source_row in _iter_normalized_rows(
                    normalized, dataset_version, candidate_record
                ):
                    final_row = _fill_normalized_geo(
                        source_row, municipalities, administrative, counters
                    )
                    payload = _canonical_json(final_row) + b"\n"
                    output_handle.write(payload)
                    normalized_digest.update(payload)
                    normalized_bytes += len(payload)
                    yield _normalized_sql_row(final_row)
                output_handle.flush()
                os.fsync(output_handle.fileno())

        connection.executemany(
            """
            INSERT INTO cep_geo (
                cep, prefix, street, complement, unit, neighborhood,
                city, uf, state, region, ibge, latitude, longitude,
                precision, method, evidence_count, evidence_radius_km, geo_source,
                evidence_digest, dataset_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows_for_insert(),
        )
        connection.commit()
        unique_ceps = connection.execute("SELECT count(*) FROM cep_geo").fetchone()[0]
        if unique_ceps != refresh["candidate_rows"]:
            raise ValueError(
                "normalized row count does not match refresh manifest: "
                f"{unique_ceps} vs {refresh['candidate_rows']}"
            )
        if counters["geo_unresolved"]:
            raise ValueError(
                f"pinned IBGE references left {counters['geo_unresolved']} unresolved rows"
            )
        if sum(counters.values()) != unique_ceps:
            raise ValueError("geography derivation counts do not match candidate rows")
        normalized_record = {
            "filename": normalized_output.name,
            "bytes": normalized_bytes,
            "sha256": normalized_digest.hexdigest(),
            "format": _EXPORT_FORMAT,
        }
        reference_codes = set(municipalities) | set(administrative)
        ibge_joined = sum(
            count
            for ibge_code, count in connection.execute(
                "SELECT ibge, count(*) FROM cep_geo GROUP BY ibge"
            )
            if ibge_code in reference_codes
        )
        located = connection.execute(
            "SELECT count(*) FROM cep_geo WHERE latitude IS NOT NULL"
        ).fetchone()[0]
        statistics: dict[str, int | float] = {
            "input_records": unique_ceps,
            "unique_ceps": unique_ceps,
            "ibge_joined": ibge_joined,
            "ibge_join_rate": round(ibge_joined / unique_ceps, 8),
            "located": located,
            "unresolved": unique_ceps - located,
            **counters,
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

        enforce_build_quality(connection, quality_policy)
        connection.row_factory = sqlite3.Row
        database_rows = iter(connection.execute("SELECT * FROM cep_geo ORDER BY cep"))
        compared = 0
        for compared, source_row in enumerate(
            _iter_normalized_rows(
                temporary_normalized, dataset_version, normalized_record
            ),
            start=1,
        ):
            database_row = next(database_rows, None)
            if database_row is None or _contract_row(database_row) != source_row:
                raise ValueError(
                    f"SQLite/normalized semantic mismatch at row {compared}"
                )
        if next(database_rows, None) is not None or compared != unique_ceps:
            raise ValueError("SQLite/normalized row count mismatch")

        metadata = [(f"count_{key}", str(value)) for key, value in statistics.items()]
        metadata.append(("normalized_export_sha256", str(normalized_record["sha256"])))
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)", metadata
        )
        connection.commit()
        connection.execute("VACUUM")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(
                f"SQLite integrity check failed before promotion: {integrity}"
            )
        connection.close()
        connection = None
        with temporary_output.open("rb") as handle:
            os.fsync(handle.fileno())

        sqlite_record = _artifact_record(temporary_output)
        sqlite_record["filename"] = output.name
        manifest_document: dict[str, object] = {
            "format": _MANIFEST_FORMAT,
            "schema_version": _SCHEMA_VERSION,
            "dataset_version": dataset_version,
            "builder": identity,
            "inputs": {
                **inherited_inputs,
                "normalized_refresh": {
                    "candidate": candidate_record,
                    "refresh_manifest": refresh_record,
                    "refresh_quality": refresh_quality_record,
                    "refresh_diff": refresh_diff_record,
                    "format": refresh["format"],
                    "status": refresh["status"],
                    "inputs": refresh["inputs"],
                    "candidate_rows": refresh["candidate_rows"],
                    "classification_counts": refresh["classification_counts"],
                    "artifacts": refresh["artifacts"],
                    "inherited_base_release": refresh["inherited_base_release"],
                    "geography_derivation": {
                        "policy": "preserve-non-null-fill-null-from-pinned-ibge-v1",
                        "candidate_located": candidate_located,
                        "candidate_unresolved": candidate_rows - candidate_located,
                        "inherited": counters["geo_inherited"],
                        "filled_municipality": counters["geo_filled_municipality"],
                        "filled_administrative_locality": counters[
                            "geo_filled_administrative"
                        ],
                        "unresolved": counters["geo_unresolved"],
                        "nearby_eligible": False,
                        "coordinate_validation": coordinate_validation,
                    },
                },
            },
            "configuration": inherited_configuration,
            "source_lock": lock_metadata,
            "sources": [
                *locked_sources,
                {
                    "id": "correios-busca-cep-v3",
                    "role": "Authoritative CEP and address refresh snapshot",
                    "required": True,
                    "attribution": (
                        "Empresa Brasileira de Correios e Telegrafos (Correios)"
                    ),
                    "snapshot": refresh["inputs"]["correios_snapshot"],
                },
            ],
            "statistics": statistics,
            "artifacts": {
                "sqlite": sqlite_record,
                "normalized": normalized_record,
            },
        }
        payload = _canonical_json(manifest_document) + b"\n"
        with temporary_manifest.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        _publish_artifact_group(
            [
                (temporary_output, output),
                (temporary_normalized, normalized_output),
                (temporary_manifest, manifest),
            ],
            force=force,
            parent_descriptor=parent_descriptor,
            parent_path=output_parent,
        )
        return {
            **statistics,
            "sqlite_sha256": sqlite_record["sha256"],
            "normalized_sha256": normalized_record["sha256"],
        }
    except Exception:
        if connection is not None:
            connection.close()
        raise
    finally:
        shutil.rmtree(staging_directory, ignore_errors=True)
        os.close(parent_descriptor)


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
    if (
        osm_observations
        and source_lock_path is not None
        and municipality_boundaries_path is None
    ):
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
                raise ValueError(
                    "locked municipality boundaries require member identities"
                )
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
        if len(selection.interior_target_municipality) + len(
            selection.boundary_target_municipality
        ) + len(selection.outside_target_municipality) + len(
            selection.unknown_cep
        ) != len(
            osm_observations
        ):
            raise ValueError("municipality boundary selection counts are inconsistent")
        boundary_selection = {
            "method": "ibge-2024-municipality-polygon-containment-v1",
            "input_observations": len(osm_observations),
            "known_target_observations": (
                len(osm_observations) - len(selection.unknown_cep)
            ),
            "eligible_observations": len(selection.eligible),
            "interior_target_municipality": len(selection.interior_target_municipality),
            "boundary_target_municipality": len(selection.boundary_target_municipality),
            "excluded_observations": len(selection.outside_target_municipality),
            "excluded_by_reason": {
                "outside_target_municipality": len(
                    selection.outside_target_municipality
                )
            },
            "outside_target_municipality": len(selection.outside_target_municipality),
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
            raise ValueError(
                f"SQLite integrity check failed before promotion: {integrity}"
            )
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
        f"file:{Path(database_path).resolve()}?mode=ro&immutable=1", uri=True
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


def lookup_prefix(database_path: str | Path, cep: object) -> dict[str, object] | None:
    cep8 = normalize_cep(cep)
    if cep8 is None:
        return None
    connection = sqlite3.connect(
        f"file:{Path(database_path).resolve()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        if metadata.get("format") != _SCHEMA_VERSION:
            raise ValueError(
                f"incompatible SQLite schema: {metadata.get('format')!r}; "
                f"expected {_SCHEMA_VERSION!r}"
            )
        target = connection.execute(
            "SELECT prefix, ibge, dataset_version FROM cep_geo WHERE cep = ?",
            (cep8,),
        ).fetchone()
        if target is None:
            return None
        members = connection.execute(
            """
            SELECT cep, latitude, longitude, precision, geo_source, evidence_digest
            FROM cep_geo
            WHERE prefix = ? AND ibge = ? AND dataset_version = ?
            ORDER BY cep
            LIMIT ?
            """,
            (
                target["prefix"],
                target["ibge"],
                target["dataset_version"],
                _PREFIX_MAX_MEMBERS + 1,
            ),
        ).fetchall()
    finally:
        connection.close()

    if len(members) > _PREFIX_MAX_MEMBERS:
        raise ValueError("same-prefix/same-IBGE membership exceeds 1000 CEPs")
    member_ceps = [row["cep"] for row in members]
    candidates = [
        row
        for row in members
        if row["cep"] != cep8
        and row["precision"] in _PREFIX_EXACT_PRECISIONS
        and row["latitude"] is not None
        and row["longitude"] is not None
    ]
    geo: dict[str, object] | None = None
    if len(candidates) >= _PREFIX_MIN_EVIDENCE:
        latitude = median(float(row["latitude"]) for row in candidates)
        longitude = median(float(row["longitude"]) for row in candidates)
        center = Point(latitude, longitude, "prefix-center")
        radius = max(
            haversine_km(
                center,
                Point(
                    float(row["latitude"]),
                    float(row["longitude"]),
                    row["cep"],
                ),
            )
            for row in candidates
        )
        if radius <= _PREFIX_MAX_EVIDENCE_RADIUS_KM:
            source_lists = [json.loads(row["geo_source"]) for row in candidates]
            if not all(
                isinstance(values, list)
                and all(isinstance(source, str) for source in values)
                for values in source_lists
            ):
                raise ValueError("prefix member sources are invalid")
            sources = sorted({source for values in source_lists for source in values})
            evidence = [
                (
                    row["cep"],
                    row["precision"],
                    float(row["latitude"]).hex(),
                    float(row["longitude"]).hex(),
                    row["evidence_digest"],
                )
                for row in candidates
            ]
            payload = json.dumps(evidence, separators=(",", ":")).encode("utf-8")
            if 1 <= len(sources) <= 16 and all(
                isinstance(source, str) and 1 <= len(source.encode("utf-8")) <= 64
                for source in sources
            ):
                geo = {
                    "type": "Point",
                    "coordinates": [longitude, latitude],
                    "precision": "observed_cep_prefix",
                    "method": "bounded_same_ibge_prefix_median",
                    "evidence_count": len(candidates),
                    "evidence_radius_km": round(radius, 3),
                    "source": sources,
                    "evidence_digest": (
                        "sha256:" + hashlib.sha256(payload).hexdigest()
                    ),
                }
    return {
        "prefix": target["prefix"],
        "ibge": target["ibge"],
        "dataset_version": target["dataset_version"],
        "member_ceps": member_ceps,
        "geo": geo,
    }
