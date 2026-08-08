from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .boundaries import select_municipality_observations
from .config import EnrichmentConfig, load_enrichment_config
from .estimator import CentroidEstimator, haversine_km, reject_outliers, robust_centroid
from .model import Observation, Point
from .sources import (
    load_ibge_municipality_references,
    load_observations,
    load_osm_observations,
)

_POLICY_FORMAT = "opencepgeo-quality-policy-v2"
_REPORT_FORMAT = "opencepgeo-quality-report-v2"
_BUILD_MANIFEST_FORMAT = "opencepgeo-build-manifest-v2"
_SCHEMA_VERSION = "opencepgeo-sqlite-v4"
_SOURCE_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_IBGE_UF = {
    "11": "RO",
    "12": "AC",
    "13": "AM",
    "14": "RR",
    "15": "PA",
    "16": "AP",
    "17": "TO",
    "21": "MA",
    "22": "PI",
    "23": "CE",
    "24": "RN",
    "25": "PB",
    "26": "PE",
    "27": "AL",
    "28": "SE",
    "29": "BA",
    "31": "MG",
    "32": "ES",
    "33": "RJ",
    "35": "SP",
    "41": "PR",
    "42": "SC",
    "43": "RS",
    "50": "MS",
    "51": "MT",
    "52": "GO",
    "53": "DF",
}


@dataclass(frozen=True, slots=True)
class QualityPolicy:
    path: Path
    document: dict[str, object]
    sha256: str

    @property
    def version(self) -> str:
        return str(self.document["version"])

    @property
    def bounds(self) -> Mapping[str, float]:
        return self.document["brazil_bounds"]  # type: ignore[return-value]

    @property
    def build_thresholds(self) -> Mapping[str, object]:
        return self.document["build_thresholds"]  # type: ignore[return-value]

    @property
    def validation(self) -> Mapping[str, object]:
        return self.document["validation"]  # type: ignore[return-value]

    @property
    def cohorts(self) -> Mapping[str, Mapping[str, object]]:
        return self.validation["cohorts"]  # type: ignore[return-value]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: str | Path, expected_format: str) -> dict[str, object]:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON document {source}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != expected_format:
        raise ValueError(f"unsupported or missing format in {source}")
    return document


def _require_number(mapping: Mapping[str, object], name: str) -> float:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"quality policy field must be numeric: {name}")
    return float(value)


def _require_integer(mapping: Mapping[str, object], name: str) -> int:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"quality policy field must be an integer: {name}")
    return value


def load_quality_policy(path: str | Path) -> QualityPolicy:
    policy_path = Path(path)
    raw = policy_path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot read quality policy {policy_path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != _POLICY_FORMAT:
        raise ValueError("unsupported or missing quality policy format")
    if not isinstance(document.get("version"), str) or not document["version"]:
        raise ValueError("quality policy version is required")
    for section in ("brazil_bounds", "build_thresholds", "validation"):
        if not isinstance(document.get(section), dict):
            raise ValueError(f"quality policy section must be an object: {section}")

    bounds = document["brazil_bounds"]
    assert isinstance(bounds, dict)
    for field in (
        "latitude_min",
        "latitude_max",
        "longitude_min",
        "longitude_max",
    ):
        _require_number(bounds, field)

    validation = document["validation"]
    assert isinstance(validation, dict)
    if validation.get("algorithm") != "sha256-modulus-v2":
        raise ValueError("unsupported holdout algorithm")
    modulus = _require_integer(validation, "modulus")
    remainder = _require_integer(validation, "remainder")
    if modulus < 2 or not 0 <= remainder < modulus:
        raise ValueError("invalid holdout modulus/remainder")
    cohorts = validation.get("cohorts")
    if not isinstance(cohorts, dict) or set(cohorts) != {
        "leave_observation_out",
        "unseen_cep",
    }:
        raise ValueError("validation cohorts must define both supported cohorts")
    for name, raw_cohort in cohorts.items():
        if not isinstance(raw_cohort, dict):
            raise ValueError(f"validation cohort must be an object: {name}")
        for field in ("minimum_records", "minimum_ufs"):
            if _require_integer(raw_cohort, field) < 0:
                raise ValueError(f"{name}.{field} must not be negative")
        for field in (
            "maximum_missing_fraction",
            "maximum_prediction_failure_fraction",
        ):
            value = _require_number(raw_cohort, field)
            if not 0 <= value <= 1:
                raise ValueError(f"{name}.{field} must be between zero and one")
        classes = raw_cohort.get("required_address_classes")
        if not isinstance(classes, list) or any(
            not isinstance(value, str) or not value for value in classes
        ):
            raise ValueError(f"{name}.required_address_classes must be strings")

    osm_evidence = validation.get("osm_evidence")
    if not isinstance(osm_evidence, dict):
        raise ValueError("validation osm_evidence must be an object")
    maximum_outside_fraction = _require_number(
        osm_evidence, "maximum_outside_target_municipality_fraction"
    )
    if not 0 <= maximum_outside_fraction <= 1:
        raise ValueError(
            "validation osm_evidence maximum_outside_target_municipality_fraction "
            "must be between zero and one"
        )

    per_uf = validation.get("per_uf")
    if not isinstance(per_uf, dict):
        raise ValueError("validation per_uf must be an object")
    if per_uf.get("cohort") not in cohorts:
        raise ValueError("validation per_uf cohort is unsupported")
    required_ufs = per_uf.get("required_ufs")
    if (
        not isinstance(required_ufs, list)
        or not required_ufs
        or len(set(required_ufs)) != len(required_ufs)
        or any(value not in _IBGE_UF.values() for value in required_ufs)
    ):
        raise ValueError("validation per_uf required_ufs must be explicit and valid")
    uf_thresholds = per_uf.get("thresholds")
    if not isinstance(uf_thresholds, dict) or set(uf_thresholds) != set(required_ufs):
        raise ValueError("validation per_uf thresholds must cover required_ufs exactly")
    for uf, threshold in uf_thresholds.items():
        if not isinstance(threshold, dict):
            raise ValueError(f"validation per_uf threshold must be an object: {uf}")
        if _require_integer(threshold, "minimum_samples") < 1:
            raise ValueError(f"validation per_uf minimum_samples must be positive: {uf}")
        if _require_number(threshold, "maximum_p95_km") <= 0:
            raise ValueError(f"validation per_uf maximum_p95_km must be positive: {uf}")

    purposes = validation.get("purposes")
    if not isinstance(purposes, dict) or not purposes:
        raise ValueError("validation purposes must be a non-empty object")
    for name, raw_purpose in purposes.items():
        if not isinstance(raw_purpose, dict) or raw_purpose.get("cohort") not in cohorts:
            raise ValueError(f"invalid purpose cohort: {name}")
        tiers = raw_purpose.get("allowed_precision_tiers")
        if not isinstance(tiers, list) or not tiers or any(
            not isinstance(value, str) or not value for value in tiers
        ):
            raise ValueError(f"invalid purpose precision tiers: {name}")
        ufs = raw_purpose.get("ufs")
        if ufs is not None and (
            not isinstance(ufs, list)
            or not ufs
            or len(set(ufs)) != len(ufs)
            or any(value not in _IBGE_UF.values() for value in ufs)
        ):
            raise ValueError(f"invalid purpose UFs: {name}")
        if _require_integer(raw_purpose, "minimum_records") < 1:
            raise ValueError(f"purpose minimum_records must be positive: {name}")
        if _require_number(raw_purpose, "maximum_p95_km") <= 0:
            raise ValueError(f"purpose maximum_p95_km must be positive: {name}")

    official = validation.get("official_pilot")
    if not isinstance(official, dict):
        raise ValueError("validation official_pilot must be an object")
    expected_ufs = official.get("expected_ufs")
    if not isinstance(expected_ufs, list) or not expected_ufs or any(
        not isinstance(value, str) or value not in _IBGE_UF.values()
        for value in expected_ufs
    ):
        raise ValueError("official_pilot expected_ufs must be explicit")
    for field in ("minimum_records",):
        if _require_integer(official, field) < 0:
            raise ValueError(f"official_pilot {field} must not be negative")
    for field in (
        "maximum_missing_fraction",
        "maximum_prediction_failure_fraction",
    ):
        value = _require_number(official, field)
        if not 0 <= value <= 1:
            raise ValueError(f"official_pilot {field} must be between zero and one")
    if _require_number(official, "maximum_p95_km") <= 0:
        raise ValueError("official_pilot maximum_p95_km must be positive")

    return QualityPolicy(
        path=policy_path,
        document=document,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _artifact_statistics(
    connection: sqlite3.Connection, bounds: Mapping[str, float]
) -> dict[str, object]:
    records = connection.execute("SELECT count(*) FROM cep_geo").fetchone()[0]
    located = connection.execute(
        "SELECT count(*) FROM cep_geo WHERE latitude IS NOT NULL"
    ).fetchone()[0]
    invalid_bounds = connection.execute(
        """
        SELECT count(*) FROM cep_geo
         WHERE latitude IS NOT NULL
           AND (latitude < ? OR latitude > ? OR longitude < ? OR longitude > ?)
        """,
        (
            bounds["latitude_min"],
            bounds["latitude_max"],
            bounds["longitude_min"],
            bounds["longitude_max"],
        ),
    ).fetchone()[0]
    tier_distribution = {
        precision or "unresolved": count
        for precision, count in connection.execute(
            "SELECT precision, count(*) FROM cep_geo GROUP BY precision"
        )
    }
    ufs = sorted(
        row[0] for row in connection.execute("SELECT DISTINCT uf FROM cep_geo")
    )
    uf_ibge_mismatches = sum(
        count
        for ibge_prefix, uf, count in connection.execute(
            "SELECT substr(ibge, 1, 2), uf, count(*) FROM cep_geo GROUP BY 1, 2"
        )
        if _IBGE_UF.get(ibge_prefix) != uf
    )
    municipality_conflicts = connection.execute(
        """
        SELECT count(*) FROM (
          SELECT ibge FROM cep_geo GROUP BY ibge
          HAVING count(DISTINCT uf) > 1
        )
        """
    ).fetchone()[0]
    municipality_label_variants = connection.execute(
        """
        SELECT count(*) FROM (
          SELECT ibge FROM cep_geo GROUP BY ibge
          HAVING count(DISTINCT city) > 1
        )
        """
    ).fetchone()[0]
    maximum_geo_source_bytes, maximum_evidence_digest_bytes = connection.execute(
        """
        SELECT coalesce(max(length(geo_source)), 0),
               coalesce(max(length(evidence_digest)), 0)
          FROM cep_geo
        """
    ).fetchone()
    invalid_evidence_digests = connection.execute(
        """
        SELECT count(*) FROM cep_geo
         WHERE latitude IS NOT NULL AND (
               evidence_digest IS NULL OR length(evidence_digest) != 71 OR
               substr(evidence_digest, 1, 7) != 'sha256:' OR
               substr(evidence_digest, 8) GLOB '*[^0-9a-f]*'
         )
        """
    ).fetchone()[0]
    return {
        "record_count": records,
        "located": located,
        "coverage": round(located / records, 8) if records else 0.0,
        "unresolved": records - located,
        "tier_distribution": dict(sorted(tier_distribution.items())),
        "ufs": ufs,
        "uf_count": len(ufs),
        "invalid_brazil_bounds": invalid_bounds,
        "uf_ibge_mismatches": uf_ibge_mismatches,
        "municipality_conflicts": municipality_conflicts,
        "municipality_label_variants": municipality_label_variants,
        "maximum_geo_source_bytes": maximum_geo_source_bytes,
        "maximum_evidence_digest_bytes": maximum_evidence_digest_bytes,
        "invalid_evidence_digests": invalid_evidence_digests,
    }


def _check_build_statistics(
    statistics: Mapping[str, object], policy: QualityPolicy
) -> list[str]:
    thresholds = policy.build_thresholds
    failures: list[str] = []
    comparisons = (
        ("record_count", "minimum_records", "minimum"),
        ("coverage", "minimum_coverage", "minimum"),
        ("unresolved", "maximum_unresolved", "maximum"),
        ("uf_count", "minimum_ufs", "minimum"),
        ("invalid_brazil_bounds", "maximum_invalid_bounds", "maximum"),
        ("uf_ibge_mismatches", "maximum_uf_ibge_mismatches", "maximum"),
        ("municipality_conflicts", "maximum_municipality_conflicts", "maximum"),
        ("maximum_geo_source_bytes", "maximum_geo_source_bytes", "maximum"),
        (
            "maximum_evidence_digest_bytes",
            "maximum_evidence_digest_bytes",
            "maximum",
        ),
        (
            "invalid_evidence_digests",
            "maximum_invalid_evidence_digests",
            "maximum",
        ),
    )
    for metric, threshold_name, direction in comparisons:
        actual = statistics[metric]
        threshold = thresholds[threshold_name]
        failed = actual < threshold if direction == "minimum" else actual > threshold
        if failed:
            failures.append(f"{metric}={actual} violates {threshold_name}={threshold}")
    allowed = set(thresholds["allowed_precision_tiers"])
    tiers = set(statistics["tier_distribution"]) - {"unresolved"}
    unexpected = sorted(tiers - allowed)
    if unexpected:
        failures.append(f"unexpected precision tier(s): {', '.join(unexpected)}")
    return failures


def enforce_build_quality(
    connection: sqlite3.Connection, policy: QualityPolicy
) -> dict[str, object]:
    statistics = _artifact_statistics(connection, policy.bounds)
    failures = _check_build_statistics(statistics, policy)
    if failures:
        raise ValueError("quality gate failed: " + "; ".join(failures))
    return statistics


def _observation_key(observation: Observation) -> bytes:
    return (
        observation.point.evidence_id
        or (
            f"{observation.cep}|{observation.point.latitude:.7f}|"
            f"{observation.point.longitude:.7f}|{observation.point.source}"
        )
    ).encode("utf-8")


def _cep_key(observation: Observation) -> bytes:
    return observation.cep.encode("ascii")


def split_holdout(
    observations: Iterable[Observation],
    policy: QualityPolicy,
    *,
    group_by_cep: bool = False,
) -> tuple[list[Observation], list[Observation]]:
    modulus = int(policy.validation["modulus"])
    remainder = int(policy.validation["remainder"])
    training: list[Observation] = []
    heldout: list[Observation] = []
    for observation in observations:
        key = _cep_key(observation) if group_by_cep else _observation_key(observation)
        bucket = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % modulus
        (heldout if bucket == remainder else training).append(observation)
    return training, heldout


def _percentiles(errors: Iterable[float]) -> dict[str, object]:
    values = sorted(errors)
    result: dict[str, object] = {"count": len(values)}
    for label, quantile in (("p50_km", 0.50), ("p90_km", 0.90), ("p95_km", 0.95)):
        index = max(0, math.ceil(len(values) * quantile) - 1)
        result[label] = round(values[index], 3) if values else None
    return result


def _group_metrics(
    samples: Iterable[tuple[str, str, str, float]],
) -> dict[str, dict[str, object]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for uf, address_class, precision, error in samples:
        groups["overall"].append(error)
        groups[f"uf:{uf}"].append(error)
        groups[f"address_class:{address_class}"].append(error)
        groups[f"precision:{precision}"].append(error)
        groups[f"uf_precision:{uf}:{precision}"].append(error)
    for precision in (
        "observed_cep",
        "osm_postcode",
        "observed_cep_prefix",
        "municipality",
    ):
        groups[f"precision:{precision}"]
    return {key: _percentiles(groups[key]) for key in sorted(groups)}


def _artifact_record(path: Path) -> dict[str, object]:
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _file_sha256(path),
    }


def _validate_build_bindings(
    *,
    connection: sqlite3.Connection,
    database: Path,
    build_manifest_path: Path,
    ibge_path: Path,
    municipality_boundaries_path: Path | None,
    osm_observations_path: Path,
    enrichment_record: Mapping[str, object],
    policy: QualityPolicy,
) -> tuple[dict[str, object], dict[str, str]]:
    manifest = _load_json(build_manifest_path, _BUILD_MANIFEST_FORMAT)
    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    if metadata.get("format") != _SCHEMA_VERSION:
        raise ValueError("quality database has incompatible schema")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("build manifest has incompatible schema")
    if manifest.get("dataset_version") != metadata.get("dataset_version"):
        raise ValueError("build manifest/database dataset version mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get("sqlite"), dict):
        raise ValueError("build manifest is missing SQLite artifact")
    if artifacts["sqlite"].get("sha256") != _file_sha256(database):
        raise ValueError("quality database does not match build manifest")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or not isinstance(inputs.get("ibge"), dict):
        raise ValueError("build manifest is missing bound IBGE input")
    if inputs["ibge"].get("sha256") != _file_sha256(ibge_path):
        raise ValueError("quality IBGE input does not match build manifest")
    boundary_input = inputs.get("municipality_boundaries")
    if municipality_boundaries_path is None:
        if boundary_input is not None:
            raise ValueError("quality municipality boundaries input is missing")
    elif not isinstance(boundary_input, dict) or boundary_input.get(
        "sha256"
    ) != _file_sha256(municipality_boundaries_path):
        raise ValueError("quality municipality boundaries do not match build manifest")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("build manifest has no configuration")
    enrichment = configuration.get("enrichment")
    quality = configuration.get("quality")
    osm = configuration.get("osm_observations")
    if (
        not isinstance(enrichment, dict)
        or enrichment.get("sha256") != enrichment_record.get("sha256")
        or metadata.get("enrichment_config_sha256") != enrichment_record.get("sha256")
    ):
        raise ValueError("quality enrichment config does not match built artifact")
    if (
        not isinstance(quality, dict)
        or quality.get("sha256") != policy.sha256
        or metadata.get("quality_config_sha256") != policy.sha256
    ):
        raise ValueError("quality policy does not match built artifact")
    osm_artifact = osm.get("artifact") if isinstance(osm, dict) else None
    if not isinstance(osm_artifact, dict) or osm_artifact.get(
        "sha256"
    ) != _file_sha256(osm_observations_path):
        raise ValueError("quality OSM evidence does not match built artifact")
    builder = manifest.get("builder")
    if not isinstance(builder, dict) or any(
        metadata.get(metadata_key) != builder.get(builder_key)
        for metadata_key, builder_key in (
            ("builder_name", "name"),
            ("builder_version", "version"),
            ("builder_source_tree_sha256", "source_tree_sha256"),
        )
    ):
        raise ValueError("build manifest/database builder identity mismatch")
    return manifest, metadata


def _estimator(
    training: Iterable[Observation],
    municipalities,
    enrichment: EnrichmentConfig,
) -> CentroidEstimator:
    return CentroidEstimator(
        (),
        municipalities,
        osm_observations=training,
        min_prefix_samples=enrichment.min_prefix_samples,
        max_prefix_radius_km=enrichment.max_prefix_radius_km,
        max_observed_radius_km=enrichment.max_observed_radius_km,
        max_osm_radius_km=enrichment.max_osm_radius_km,
        max_osm_municipality_distance_km=enrichment.max_osm_municipality_distance_km,
        outlier_min_samples=enrichment.outlier_min_samples,
        outlier_mad_multiplier=enrichment.outlier_mad_multiplier,
        outlier_floor_km=enrichment.outlier_floor_km,
    )


def _evaluate(
    estimator: CentroidEstimator,
    heldout: Iterable[Observation],
    metadata: Mapping[
        str,
        tuple[str, str, str | None, str | None, float | None, float | None, str | None],
    ],
) -> tuple[list[tuple[str, str, str, float]], int, int]:
    samples: list[tuple[str, str, str, float]] = []
    missing_ceps = 0
    prediction_failures = 0
    for observation in heldout:
        row = metadata.get(observation.cep)
        if row is None:
            missing_ceps += 1
            continue
        ibge, uf, street, neighborhood, _latitude, _longitude, _precision = row
        estimate = estimator.estimate(observation.cep, ibge)
        if estimate is None:
            prediction_failures += 1
            continue
        address_class = (
            "urban_address_proxy"
            if street or neighborhood
            else "rural_or_general_address_proxy"
        )
        error = haversine_km(
            Point(estimate.latitude, estimate.longitude, "prediction"),
            observation.point,
        )
        samples.append((uf, address_class, estimate.precision, error))
    return samples, missing_ceps, prediction_failures


def _fraction(value: int, total: int) -> float:
    return round(value / total, 8) if total else 0.0


def _evidence_selection_statistics(
    *,
    total: int,
    polygon_eligible: int,
    interior: int,
    boundary: int,
    outside: int,
    unknown: int,
) -> dict[str, object]:
    known = total - unknown
    if (
        min(total, polygon_eligible, interior, boundary, outside, unknown) < 0
        or interior + boundary + outside != known
        or polygon_eligible != interior + boundary + unknown
    ):
        raise ValueError("municipality boundary selection counts are inconsistent")
    return {
        "input_observations": total,
        "known_target_observations": known,
        "polygon_eligible_observations": polygon_eligible,
        "interior_target_municipality": interior,
        "boundary_target_municipality": boundary,
        "outside_target_municipality": outside,
        "outside_target_municipality_fraction": _fraction(outside, known),
        "unknown_cep_observations_retained_for_missingness": unknown,
    }


def _production_eligible_osm(
    observations: Iterable[Observation],
    metadata: Mapping[
        str,
        tuple[str, str, str | None, str | None, float | None, float | None, str | None],
    ],
    municipalities,
    enrichment: EnrichmentConfig,
) -> tuple[list[Observation], dict[str, int]]:
    groups: dict[str, list[Observation]] = defaultdict(list)
    retained_unknown: list[Observation] = []
    for observation in observations:
        if observation.cep not in metadata:
            retained_unknown.append(observation)
        else:
            groups[observation.cep].append(observation)

    retained = list(retained_unknown)
    excluded = {
        "outside_reference_distance_backstop": 0,
        "robust_spatial_outlier": 0,
        "cep_group_radius_rejection": 0,
    }
    for cep in sorted(groups):
        group = groups[cep]
        municipality = municipalities.get(metadata[cep][0])
        if municipality is None:
            excluded["outside_reference_distance_backstop"] += len(group)
            continue
        corroborated = [
            observation
            for observation in group
            if haversine_km(observation.point, municipality.point)
            <= enrichment.max_osm_municipality_distance_km
        ]
        excluded["outside_reference_distance_backstop"] += len(group) - len(
            corroborated
        )
        retained_points = reject_outliers(
            (observation.point for observation in corroborated),
            minimum_samples=enrichment.outlier_min_samples,
            mad_multiplier=enrichment.outlier_mad_multiplier,
            floor_km=enrichment.outlier_floor_km,
        )
        retained_identities = {
            point.evidence_id or point.source for point in retained_points
        }
        filtered = [
            observation
            for observation in corroborated
            if (observation.point.evidence_id or observation.point.source)
            in retained_identities
        ]
        excluded["robust_spatial_outlier"] += len(corroborated) - len(filtered)
        if filtered:
            estimate = robust_centroid(
                (observation.point for observation in filtered),
                "osm_postcode",
                "robust_median_osm_postcode",
            )
            if estimate.evidence_radius_km <= enrichment.max_osm_radius_km:
                retained.extend(filtered)
                continue
        excluded["cep_group_radius_rejection"] += len(filtered)
    return retained, excluded


def _gated_cohort_inputs(
    polygon_eligible: list[Observation],
    production_eligible: list[Observation],
) -> dict[str, list[Observation]]:
    return {
        "leave_observation_out": production_eligible,
        "unseen_cep": polygon_eligible,
    }


def _cohort_document(
    *,
    name: str,
    training: list[Observation],
    heldout: list[Observation],
    samples: list[tuple[str, str, str, float]],
    missing_ceps: int,
    prediction_failures: int,
    policy: QualityPolicy,
) -> dict[str, object]:
    ufs = sorted({sample[0] for sample in samples})
    return {
        "split_unit": "cep" if name == "unseen_cep" else "observation",
        "training_observations": len(training),
        "heldout_observations": len(heldout),
        "heldout_ceps": len({observation.cep for observation in heldout}),
        "evaluated_observations": len(samples),
        "missing_ceps": missing_ceps,
        "missing_fraction": _fraction(missing_ceps, len(heldout)),
        "prediction_failures": prediction_failures,
        "prediction_failure_fraction": _fraction(prediction_failures, len(heldout)),
        "ufs": ufs,
        "missing_ufs": sorted(set(_IBGE_UF.values()) - set(ufs)),
        "address_classes": sorted({sample[1] for sample in samples}),
        "metrics": _group_metrics(samples),
        "algorithm": policy.validation["algorithm"],
        "modulus": policy.validation["modulus"],
        "remainder": policy.validation["remainder"],
        "interpretation": (
            "same-CEP centroid consistency proxy; source-correlated mapping errors remain possible"
            if name == "leave_observation_out"
            else "unseen-CEP fallback quality; every OSM observation for a held-out CEP is excluded from training"
        ),
    }


def _add_check(
    checks: list[dict[str, object]],
    name: str,
    passed: bool,
    actual: object,
    threshold: object,
) -> None:
    checks.append(
        {"name": name, "passed": passed, "actual": actual, "threshold": threshold}
    )


def _report_checks(
    *,
    artifact: Mapping[str, object],
    cohorts: Mapping[str, Mapping[str, object]],
    cohort_samples: Mapping[str, list[tuple[str, str, str, float]]],
    official: Mapping[str, object],
    evidence_selection: Mapping[str, object],
    policy: QualityPolicy,
) -> tuple[list[dict[str, object]], list[str], dict[str, dict[str, object]]]:
    checks: list[dict[str, object]] = []
    build_failures = _check_build_statistics(artifact, policy)
    _add_check(checks, "artifact_build_policy", not build_failures, build_failures, [])
    osm_policy = policy.validation["osm_evidence"]
    assert isinstance(osm_policy, dict)
    _add_check(
        checks,
        "osm_evidence.maximum_outside_target_municipality_fraction",
        evidence_selection["outside_target_municipality_fraction"]
        <= osm_policy["maximum_outside_target_municipality_fraction"],
        evidence_selection["outside_target_municipality_fraction"],
        osm_policy["maximum_outside_target_municipality_fraction"],
    )

    for name in ("leave_observation_out", "unseen_cep"):
        cohort = cohorts[name]
        threshold = policy.cohorts[name]
        _add_check(
            checks,
            f"{name}.minimum_records",
            cohort["evaluated_observations"] >= threshold["minimum_records"],
            cohort["evaluated_observations"],
            threshold["minimum_records"],
        )
        _add_check(
            checks,
            f"{name}.maximum_missing_fraction",
            cohort["missing_fraction"] <= threshold["maximum_missing_fraction"],
            cohort["missing_fraction"],
            threshold["maximum_missing_fraction"],
        )
        _add_check(
            checks,
            f"{name}.maximum_prediction_failure_fraction",
            cohort["prediction_failure_fraction"]
            <= threshold["maximum_prediction_failure_fraction"],
            cohort["prediction_failure_fraction"],
            threshold["maximum_prediction_failure_fraction"],
        )
        _add_check(
            checks,
            f"{name}.minimum_ufs",
            len(cohort["ufs"]) >= threshold["minimum_ufs"],
            len(cohort["ufs"]),
            threshold["minimum_ufs"],
        )
        required_classes = set(threshold["required_address_classes"])
        actual_classes = set(cohort["address_classes"])
        _add_check(
            checks,
            f"{name}.required_address_classes",
            required_classes <= actual_classes,
            sorted(actual_classes),
            sorted(required_classes),
        )

    per_uf = policy.validation["per_uf"]
    assert isinstance(per_uf, dict)
    per_uf_cohort = str(per_uf["cohort"])
    per_uf_metrics = cohorts[per_uf_cohort]["metrics"]
    assert isinstance(per_uf_metrics, dict)
    for uf in sorted(per_uf["required_ufs"]):
        uf_threshold = per_uf["thresholds"][uf]
        metric = per_uf_metrics.get(f"uf:{uf}", {})
        count = metric.get("count", 0) if isinstance(metric, dict) else 0
        p95 = metric.get("p95_km") if isinstance(metric, dict) else None
        _add_check(
            checks,
            f"per_uf.{uf}.minimum_samples",
            isinstance(count, int) and count >= uf_threshold["minimum_samples"],
            count,
            uf_threshold["minimum_samples"],
        )
        _add_check(
            checks,
            f"per_uf.{uf}.maximum_p95_km",
            isinstance(p95, (int, float))
            and p95 <= uf_threshold["maximum_p95_km"],
            p95,
            uf_threshold["maximum_p95_km"],
        )

    purpose_metrics: dict[str, dict[str, object]] = {}
    purposes = policy.validation["purposes"]
    assert isinstance(purposes, dict)
    for name in sorted(purposes):
        purpose = purposes[name]
        assert isinstance(purpose, dict)
        allowed = set(purpose["allowed_precision_tiers"])
        allowed_ufs = set(purpose.get("ufs", _IBGE_UF.values()))
        values = [
            sample[3]
            for sample in cohort_samples[str(purpose["cohort"])]
            if sample[0] in allowed_ufs and sample[2] in allowed
        ]
        metric = _percentiles(values)
        metric["cohort"] = purpose["cohort"]
        metric["allowed_precision_tiers"] = sorted(allowed)
        metric["ufs"] = sorted(allowed_ufs)
        purpose_metrics[name] = metric
        _add_check(
            checks,
            f"purpose.{name}.minimum_records",
            metric["count"] >= purpose["minimum_records"],
            metric["count"],
            purpose["minimum_records"],
        )
        p95 = metric["p95_km"]
        _add_check(
            checks,
            f"purpose.{name}.maximum_p95_km",
            isinstance(p95, (int, float)) and p95 <= purpose["maximum_p95_km"],
            p95,
            purpose["maximum_p95_km"],
        )

    official_policy = policy.validation["official_pilot"]
    assert isinstance(official_policy, dict)
    official_metrics = official["metrics"]
    assert isinstance(official_metrics, dict)
    official_p95 = official_metrics.get("overall", {}).get("p95_km")
    official_checks = (
        (
            "official_pilot.minimum_records",
            official["evaluated_observations"] >= official_policy["minimum_records"],
            official["evaluated_observations"],
            official_policy["minimum_records"],
        ),
        (
            "official_pilot.maximum_missing_fraction",
            official["missing_fraction"] <= official_policy["maximum_missing_fraction"],
            official["missing_fraction"],
            official_policy["maximum_missing_fraction"],
        ),
        (
            "official_pilot.maximum_prediction_failure_fraction",
            official["prediction_failure_fraction"]
            <= official_policy["maximum_prediction_failure_fraction"],
            official["prediction_failure_fraction"],
            official_policy["maximum_prediction_failure_fraction"],
        ),
        (
            "official_pilot.expected_ufs",
            official["ufs"] == sorted(official_policy["expected_ufs"]),
            official["ufs"],
            sorted(official_policy["expected_ufs"]),
        ),
        (
            "official_pilot.maximum_p95_km",
            isinstance(official_p95, (int, float))
            and official_p95 <= official_policy["maximum_p95_km"],
            official_p95,
            official_policy["maximum_p95_km"],
        ),
    )
    for args in official_checks:
        _add_check(checks, *args)

    failures = [str(check["name"]) for check in checks if not check["passed"]]
    return checks, failures, purpose_metrics


def expected_quality_check_names(policy: QualityPolicy) -> set[str]:
    names = {
        "artifact_build_policy",
        "osm_evidence.maximum_outside_target_municipality_fraction",
    }
    for cohort in ("leave_observation_out", "unseen_cep"):
        names.update(
            {
                f"{cohort}.minimum_records",
                f"{cohort}.maximum_missing_fraction",
                f"{cohort}.maximum_prediction_failure_fraction",
                f"{cohort}.minimum_ufs",
                f"{cohort}.required_address_classes",
            }
        )
    per_uf = policy.validation["per_uf"]
    assert isinstance(per_uf, dict)
    for uf in per_uf["required_ufs"]:
        names.add(f"per_uf.{uf}.minimum_samples")
        names.add(f"per_uf.{uf}.maximum_p95_km")
    purposes = policy.validation["purposes"]
    assert isinstance(purposes, dict)
    for purpose in purposes:
        names.add(f"purpose.{purpose}.minimum_records")
        names.add(f"purpose.{purpose}.maximum_p95_km")
    names.update(
        {
            "official_pilot.minimum_records",
            "official_pilot.maximum_missing_fraction",
            "official_pilot.maximum_prediction_failure_fraction",
            "official_pilot.expected_ufs",
            "official_pilot.maximum_p95_km",
        }
    )
    return names


def validate_quality_report(
    report: Mapping[str, object],
    policy: QualityPolicy,
    *,
    expected_dataset_version: str,
    expected_inputs: Mapping[str, object] | None = None,
) -> None:
    required_top_level = {
        "format",
        "quality_version",
        "dataset_version",
        "status",
        "failures",
        "inputs",
        "artifact",
        "evidence_selection",
        "uncensored_diagnostics",
        "cohorts",
        "purposes",
        "official_pilot",
        "checks",
        "certification",
        "evidence_gaps",
    }
    if set(report) != required_top_level:
        raise ValueError("quality report has incomplete or unexpected top-level fields")
    if report.get("format") != _REPORT_FORMAT:
        raise ValueError("quality report has incompatible format")
    if report.get("quality_version") != policy.version:
        raise ValueError("quality report policy version mismatch")
    if report.get("dataset_version") != expected_dataset_version:
        raise ValueError("quality report dataset version mismatch")
    inputs = report.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("quality report inputs must be an object")
    required_inputs = {
        "database_sha256",
        "build_manifest_sha256",
        "ibge_sha256",
        "osm_observations_sha256",
        "official_holdout_sha256",
        "official_holdout_source_id",
        "official_holdout_filename",
        "official_holdout_bytes",
        "official_holdout_path_contract",
        "municipality_boundaries_sha256",
        "quality_policy_sha256",
        "enrichment_config_sha256",
    }
    if set(inputs) != required_inputs:
        raise ValueError("quality report input bindings are incomplete")
    if inputs.get("quality_policy_sha256") != policy.sha256:
        raise ValueError("quality report policy hash mismatch")
    if (
        not isinstance(inputs.get("official_holdout_source_id"), str)
        or _SOURCE_ID.fullmatch(inputs["official_holdout_source_id"]) is None
        or not isinstance(inputs.get("official_holdout_filename"), str)
        or Path(inputs["official_holdout_filename"]).name
        != inputs["official_holdout_filename"]
        or not isinstance(inputs.get("official_holdout_bytes"), int)
        or inputs["official_holdout_bytes"] < 1
        or inputs.get("official_holdout_path_contract")
        != "caller-supplied-private-local-file-not-packaged"
    ):
        raise ValueError("quality report official holdout identity is invalid")
    if expected_inputs is not None and any(
        inputs.get(name) != value for name, value in expected_inputs.items()
    ):
        raise ValueError("quality report input hash mismatch")
    checks = report.get("checks")
    if not isinstance(checks, list):
        raise ValueError("quality report checks must be a list")
    actual_names: set[str] = set()
    failed_names: list[str] = []
    for check in checks:
        if not isinstance(check, dict) or set(check) != {
            "name",
            "passed",
            "actual",
            "threshold",
        }:
            raise ValueError("quality report contains a malformed check")
        name = check.get("name")
        if not isinstance(name, str) or name in actual_names:
            raise ValueError("quality report contains duplicate or invalid checks")
        if not isinstance(check.get("passed"), bool):
            raise ValueError("quality report check result must be boolean")
        actual_names.add(name)
        if not check["passed"]:
            failed_names.append(name)
    if actual_names != expected_quality_check_names(policy):
        raise ValueError("quality report check set is incomplete or unexpected")
    failures = report.get("failures")
    if failures != failed_names:
        raise ValueError("quality report failures do not match check results")
    expected_status = "pass" if not failed_names else "fail"
    if report.get("status") != expected_status:
        raise ValueError("quality report status does not match check results")
    certification = report.get("certification")
    if not isinstance(certification, dict) or certification != {
        "national_official_validation": False,
        "nearby_store_position_error_calibrated": False,
        "official_scope": "BA-only pilot",
    }:
        raise ValueError("quality report certification scope is invalid")


def build_quality_report(
    *,
    database_path: str | Path,
    build_manifest_path: str | Path,
    ibge_path: str | Path,
    osm_observations_path: str | Path,
    official_holdout_path: str | Path,
    official_holdout_source_id: str,
    municipality_boundaries_path: str | Path | None = None,
    enrichment_config_path: str | Path,
    quality_policy_path: str | Path,
) -> dict[str, object]:
    policy = load_quality_policy(quality_policy_path)
    enrichment, enrichment_record = load_enrichment_config(enrichment_config_path)
    database = Path(database_path)
    build_manifest = Path(build_manifest_path)
    ibge = Path(ibge_path)
    osm_path = Path(osm_observations_path)
    official_path = Path(official_holdout_path)
    if _SOURCE_ID.fullmatch(official_holdout_source_id) is None:
        raise ValueError("official holdout source ID is invalid")
    if not official_path.name:
        raise ValueError("official holdout path has no filename")
    boundaries_path = (
        Path(municipality_boundaries_path)
        if municipality_boundaries_path is not None
        else None
    )
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        build_manifest_document, metadata_record = _validate_build_bindings(
            connection=connection,
            database=database,
            build_manifest_path=build_manifest,
            ibge_path=ibge,
            municipality_boundaries_path=boundaries_path,
            osm_observations_path=osm_path,
            enrichment_record=enrichment_record,
            policy=policy,
        )
        artifact = _artifact_statistics(connection, policy.bounds)
        dataset_version = metadata_record["dataset_version"]
        osm = load_osm_observations(osm_path)
        official_observations = load_observations(official_path)
        municipalities = load_ibge_municipality_references(ibge)
        metadata_ceps = {observation.cep for observation in osm} | {
            observation.cep for observation in official_observations
        }
        metadata: dict[
            str,
            tuple[
                str,
                str,
                str | None,
                str | None,
                float | None,
                float | None,
                str | None,
            ],
        ] = {}
        for row in connection.execute(
            """
            SELECT cep, ibge, uf, street, neighborhood,
                   latitude, longitude, precision
              FROM cep_geo
            """
        ):
            if row["cep"] in metadata_ceps:
                metadata[row["cep"]] = (
                    row["ibge"],
                    row["uf"],
                    row["street"],
                    row["neighborhood"],
                    row["latitude"],
                    row["longitude"],
                    row["precision"],
                )

        expected_boundary_members = None
        if boundaries_path is not None:
            manifest_inputs = build_manifest_document.get("inputs")
            boundary_input = (
                manifest_inputs.get("municipality_boundaries")
                if isinstance(manifest_inputs, dict)
                else None
            )
            expected_boundary_members = (
                boundary_input.get("members")
                if isinstance(boundary_input, dict)
                else None
            )
            if not isinstance(expected_boundary_members, dict):
                raise ValueError(
                    "build manifest municipality boundaries lack member identities"
                )
        selection = (
            select_municipality_observations(
                boundaries_path,
                osm,
                {cep: row[0] for cep, row in metadata.items()},
                expected_members=expected_boundary_members,
            )
            if boundaries_path is not None
            else None
        )
        polygon_eligible_osm = (
            list(selection.eligible) if selection is not None else osm
        )
        outside_count = (
            len(selection.outside_target_municipality)
            if selection is not None
            else 0
        )
        unknown_count = len(selection.unknown_cep) if selection is not None else 0
        interior_count = (
            len(selection.interior_target_municipality)
            if selection is not None
            else len(osm)
        )
        boundary_count = (
            len(selection.boundary_target_municipality)
            if selection is not None
            else 0
        )
        selection_statistics = _evidence_selection_statistics(
            total=len(osm),
            polygon_eligible=len(polygon_eligible_osm),
            interior=interior_count,
            boundary=boundary_count,
            outside=outside_count,
            unknown=unknown_count,
        )
        eligible_osm, estimator_exclusions = _production_eligible_osm(
            polygon_eligible_osm,
            metadata,
            municipalities,
            enrichment,
        )
        excluded_by_reason = {
            "outside_target_municipality": outside_count,
            **estimator_exclusions,
        }
        if sum(excluded_by_reason.values()) != len(osm) - len(eligible_osm):
            raise ValueError("OSM evidence exclusion counts are inconsistent")
        evidence_selection = {
            "method": (
                "ibge-2024-municipality-polygon-containment-v1"
                if selection is not None
                else "not-configured-fixture-only"
            ),
            **selection_statistics,
            "eligible_observations": len(eligible_osm),
            "excluded_observations": len(osm) - len(eligible_osm),
            "excluded_by_reason": excluded_by_reason,
            "interpretation": (
                "OSM points outside the official target municipality polygon are "
                "excluded, then the estimator's reference-distance, robust-outlier, "
                "and CEP-radius rules select production-retained LOO evidence; the "
                "unseen cohort uses all polygon-contained evidence, and unknown CEPs "
                "remain to measure missingness"
            ),
        }
        cohort_inputs = _gated_cohort_inputs(
            polygon_eligible_osm, eligible_osm
        )
        splits = {
            "leave_observation_out": split_holdout(
                cohort_inputs["leave_observation_out"], policy
            ),
            "unseen_cep": split_holdout(
                cohort_inputs["unseen_cep"], policy, group_by_cep=True
            ),
        }
        cohort_documents: dict[str, dict[str, object]] = {}
        cohort_samples: dict[str, list[tuple[str, str, str, float]]] = {}
        for name, (training, heldout) in splits.items():
            samples, missing, failures = _evaluate(
                _estimator(training, municipalities, enrichment), heldout, metadata
            )
            cohort_samples[name] = samples
            cohort_documents[name] = _cohort_document(
                name=name,
                training=training,
                heldout=heldout,
                samples=samples,
                missing_ceps=missing,
                prediction_failures=failures,
                policy=policy,
            )
            cohort_documents[name]["evidence_scope"] = (
                "official-polygon-contained and production-retained full CEP groups"
                if name == "leave_observation_out"
                else "all official-polygon-contained observations; estimator training applies production filters internally"
            )

        uncensored_diagnostics: dict[str, dict[str, object]] = {}
        for raw_name, group_by_cep in (
            ("leave_observation_out", False),
            ("unseen_cep", True),
        ):
            raw_training, raw_heldout = split_holdout(
                osm, policy, group_by_cep=group_by_cep
            )
            raw_samples, raw_missing, raw_failures = _evaluate(
                _estimator(raw_training, municipalities, enrichment),
                raw_heldout,
                metadata,
            )
            raw_document = _cohort_document(
                name=raw_name,
                training=raw_training,
                heldout=raw_heldout,
                samples=raw_samples,
                missing_ceps=raw_missing,
                prediction_failures=raw_failures,
                policy=policy,
            )
            raw_document["interpretation"] = (
                "diagnostic only, not gated: raw OSM evidence before official "
                "municipality polygon and full-group estimator eligibility filtering; "
                "this is consistency against community-mapped evidence, not positional accuracy"
            )
            uncensored_diagnostics[raw_name] = raw_document

        official_samples: list[tuple[str, str, str, float]] = []
        official_missing = 0
        official_prediction_failures = 0
        for observation in official_observations:
            row = metadata.get(observation.cep)
            if row is None:
                official_missing += 1
                continue
            _ibge, uf, street, neighborhood, latitude, longitude, precision = row
            if latitude is None or longitude is None or precision is None:
                official_prediction_failures += 1
                continue
            address_class = (
                "urban_address_proxy"
                if street or neighborhood
                else "rural_or_general_address_proxy"
            )
            official_samples.append(
                (
                    uf,
                    address_class,
                    precision,
                    haversine_km(
                        Point(latitude, longitude, "prediction"), observation.point
                    ),
                )
            )
        official_total = len(official_observations)
        official = {
            "source": "SEFAZ-BA/PRODEB Preço da Hora captured offline pilot samples",
            "source_id": official_holdout_source_id,
            "scope": "BA-only pilot; not nationally representative",
            "input_observations": official_total,
            "evaluated_observations": len(official_samples),
            "missing_ceps": official_missing,
            "missing_fraction": _fraction(official_missing, official_total),
            "prediction_failures": official_prediction_failures,
            "prediction_failure_fraction": _fraction(
                official_prediction_failures, official_total
            ),
            "ufs": sorted({sample[0] for sample in official_samples}),
            "metrics": _group_metrics(official_samples),
            "independence": "coordinates were not supplied to the OpenCEPGeo builder",
            "distribution": "internal validation summary only; source terms do not establish dataset redistribution rights",
        }
        checks, failures, purposes = _report_checks(
            artifact=artifact,
            cohorts=cohort_documents,
            cohort_samples=cohort_samples,
            official=official,
            evidence_selection=evidence_selection,
            policy=policy,
        )
        report: dict[str, object] = {
            "format": _REPORT_FORMAT,
            "quality_version": policy.version,
            "dataset_version": dataset_version,
            "status": "pass" if not failures else "fail",
            "failures": failures,
            "inputs": {
                "database_sha256": _file_sha256(database),
                "build_manifest_sha256": _file_sha256(build_manifest),
                "ibge_sha256": _file_sha256(ibge),
                "osm_observations_sha256": _file_sha256(osm_path),
                "official_holdout_sha256": _file_sha256(official_path),
                "official_holdout_source_id": official_holdout_source_id,
                "official_holdout_filename": official_path.name,
                "official_holdout_bytes": official_path.stat().st_size,
                "official_holdout_path_contract": "caller-supplied-private-local-file-not-packaged",
                "municipality_boundaries_sha256": (
                    _file_sha256(boundaries_path)
                    if boundaries_path is not None
                    else None
                ),
                "quality_policy_sha256": policy.sha256,
                "enrichment_config_sha256": enrichment_record["sha256"],
            },
            "artifact": artifact,
            "evidence_selection": evidence_selection,
            "uncensored_diagnostics": uncensored_diagnostics,
            "cohorts": cohort_documents,
            "purposes": purposes,
            "official_pilot": official,
            "checks": checks,
            "certification": {
                "national_official_validation": False,
                "nearby_store_position_error_calibrated": False,
                "official_scope": "BA-only pilot",
            },
            "evidence_gaps": [
                "The independent official pilot covers BA only and does not certify national accuracy.",
                "The leave-observation-out cohort measures same-CEP OSM consistency, not independent ground-truth accuracy.",
                "The unseen-CEP cohort measures fallback behavior using OSM as the reference and remains community-mapped evidence.",
                "The gated LOO cohort conditions on full-group estimator eligibility, which can introduce selection bias; gated unseen CEPs use all polygon-contained evidence, and both raw-OSM cohorts remain visible and uncensored.",
                "Municipality containment uses the independently locked official IBGE 2024 polygon dataset; polygons are still generalized cartographic boundaries.",
                "evidence_radius_km measures retained evidence spread and is not a calibrated positional-error bound.",
                "No approved production first-party corpus exists for observed_cep or observed_cep_prefix calibration.",
            ],
        }
        validate_quality_report(
            report,
            policy,
            expected_dataset_version=dataset_version,
            expected_inputs=report["inputs"],  # type: ignore[arg-type]
        )
        return report
    finally:
        connection.close()


def write_quality_report(report: Mapping[str, object], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    output.write_text(payload, encoding="utf-8")


def quality_report_markdown(report: Mapping[str, object]) -> str:
    artifact = report["artifact"]
    cohorts = report["cohorts"]
    evidence_selection = report["evidence_selection"]
    uncensored = report["uncensored_diagnostics"]
    official = report["official_pilot"]
    purposes = report["purposes"]
    assert isinstance(artifact, dict)
    assert isinstance(cohorts, dict)
    assert isinstance(evidence_selection, dict)
    assert isinstance(uncensored, dict)
    assert isinstance(official, dict)
    assert isinstance(purposes, dict)
    lines = [
        f"# OpenCEPGeo quality report {report['quality_version']}",
        "",
        f"Status: **{str(report['status']).upper()}**",
        "",
        "> Scope: the independent official evidence is a BA-only pilot. This report does not certify national official accuracy or calibrated nearby-store positional error.",
        "",
        "## Artifact coverage",
        "",
        f"- Records: {artifact['record_count']}",
        f"- Located: {artifact['located']} ({artifact['coverage']:.6%})",
        f"- Unresolved: {artifact['unresolved']}",
        f"- UFs: {artifact['uf_count']}/27",
        f"- Maximum serialized source categories: {artifact['maximum_geo_source_bytes']} bytes",
        f"- Evidence digest: maximum {artifact['maximum_evidence_digest_bytes']} bytes, invalid rows {artifact['invalid_evidence_digests']}",
        f"- Tier distribution: `{json.dumps(artifact['tier_distribution'], sort_keys=True)}`",
        "",
        "## OSM evidence eligibility",
        "",
        f"- Input observations: {evidence_selection['input_observations']}",
        f"- Known-target observations: {evidence_selection['known_target_observations']}",
        f"- Polygon-eligible observations: {evidence_selection['polygon_eligible_observations']}",
        f"- Eligible observations: {evidence_selection['eligible_observations']}",
        f"- Interior/boundary observations: {evidence_selection['interior_target_municipality']}/{evidence_selection['boundary_target_municipality']}",
        f"- Outside target municipality: {evidence_selection['outside_target_municipality']} ({evidence_selection['outside_target_municipality_fraction']:.6%})",
        f"- Unknown CEP observations retained for missingness: {evidence_selection['unknown_cep_observations_retained_for_missingness']}",
        f"- Exclusions by reason: `{json.dumps(evidence_selection['excluded_by_reason'], sort_keys=True)}`",
        f"- Method: {evidence_selection['method']}",
        f"- Interpretation: {evidence_selection['interpretation']}",
    ]
    for cohort_name in ("leave_observation_out", "unseen_cep"):
        cohort = cohorts[cohort_name]
        metrics = cohort["metrics"]
        title = (
            "Leave-observation-out same-CEP consistency"
            if cohort_name == "leave_observation_out"
            else "CEP-group holdout for unseen-CEP fallback"
        )
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                f"- Interpretation: {cohort['interpretation']}",
                f"- Evidence scope: {cohort['evidence_scope']}",
                f"- Training observations: {cohort['training_observations']}",
                f"- Held-out observations: {cohort['heldout_observations']}",
                f"- Held-out CEPs: {cohort['heldout_ceps']}",
                f"- Evaluated observations: {cohort['evaluated_observations']}",
                f"- Missing/prediction failures: {cohort['missing_ceps']}/{cohort['prediction_failures']}",
                f"- UFs: {len(cohort['ufs'])}/27",
                "",
                "| Group | Count | p50 km | p90 km | p95 km |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for key in (
            "overall",
            "precision:observed_cep",
            "precision:osm_postcode",
            "precision:observed_cep_prefix",
            "precision:municipality",
        ):
            metric = metrics.get(key)
            if isinstance(metric, dict):
                lines.append(
                    f"| {key} | {metric['count']} | {metric['p50_km']} | "
                    f"{metric['p90_km']} | {metric['p95_km']} |"
                )

    for raw_name in ("leave_observation_out", "unseen_cep"):
        raw_document = uncensored[raw_name]
        raw_metrics = raw_document["metrics"]
        lines.extend(
            [
                "",
                f"## Uncensored raw-OSM {raw_name} diagnostic (not gated)",
                "",
                f"- Interpretation: {raw_document['interpretation']}",
                f"- Held-out observations: {raw_document['heldout_observations']}",
                f"- Evaluated observations: {raw_document['evaluated_observations']}",
                f"- Overall p95: {raw_metrics['overall']['p95_km']} km",
                f"- RR OSM-tier p95: {raw_metrics.get('uf_precision:RR:osm_postcode', {}).get('p95_km')} km",
            ]
        )

    lines.extend(["", "## Purpose gates", ""])
    for name in sorted(purposes):
        metric = purposes[name]
        lines.append(
            f"- `{name}` ({metric['cohort']}, {metric['allowed_precision_tiers']}): "
            f"UFs {metric['ufs']}, count {metric['count']}, p95 {metric['p95_km']} km"
        )
    lines.extend(
        [
            "",
            "## Independent official BA-only pilot",
            "",
            f"- Evidence source ID: `{official['source_id']}`",
            f"- Evidence artifact: `{report['inputs']['official_holdout_filename']}` "
            f"({report['inputs']['official_holdout_bytes']} bytes; private local input, not packaged)",
            f"- Scope: {official['scope']}",
            f"- Evaluated observations: {official['evaluated_observations']}",
            f"- Missing/prediction failures: {official['missing_ceps']}/{official['prediction_failures']}",
            f"- UFs: {', '.join(official['ufs'])}",
            "",
            "| Group | Count | p50 km | p90 km | p95 km |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    official_metrics = official["metrics"]
    for key in sorted(official_metrics):
        if key == "overall" or key.startswith("precision:"):
            metric = official_metrics[key]
            lines.append(
                f"| {key} | {metric['count']} | {metric['p50_km']} | "
                f"{metric['p90_km']} | {metric['p95_km']} |"
            )
    lines.extend(["", "## Evidence gaps", ""])
    lines.extend(f"- {gap}" for gap in report["evidence_gaps"])
    lines.extend(["", "## Gate checks", ""])
    for check in report["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        lines.append(
            f"- **{marker}** `{check['name']}`: actual `{check['actual']}`, threshold `{check['threshold']}`"
        )
    return "\n".join(lines) + "\n"
