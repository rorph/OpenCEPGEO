from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import load_enrichment_config
from .estimator import CentroidEstimator, haversine_km
from .model import Observation, Point
from .sources import (
    load_ibge_municipality_references,
    load_observations,
    load_osm_observations,
)

_POLICY_FORMAT = "opencepgeo-quality-policy-v1"
_REPORT_FORMAT = "opencepgeo-quality-report-v1"
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
    def holdout(self) -> Mapping[str, object]:
        return self.document["holdout"]  # type: ignore[return-value]

    @property
    def error_thresholds(self) -> Mapping[str, float]:
        return self.document["error_thresholds_km"]  # type: ignore[return-value]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_quality_policy(path: str | Path) -> QualityPolicy:
    policy_path = Path(path)
    try:
        raw = policy_path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read quality policy {policy_path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != _POLICY_FORMAT:
        raise ValueError("unsupported or missing quality policy format")
    if not isinstance(document.get("version"), str) or not document["version"]:
        raise ValueError("quality policy version is required")
    for section in (
        "brazil_bounds",
        "build_thresholds",
        "holdout",
        "error_thresholds_km",
    ):
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
        if not isinstance(bounds.get(field), (int, float)):
            raise ValueError(f"quality policy bound must be numeric: {field}")
    holdout = document["holdout"]
    assert isinstance(holdout, dict)
    if holdout.get("algorithm") != "sha256-modulus-v1":
        raise ValueError("unsupported holdout algorithm")
    modulus = holdout.get("modulus")
    remainder = holdout.get("remainder")
    if (
        not isinstance(modulus, int)
        or modulus < 2
        or not isinstance(remainder, int)
        or not 0 <= remainder < modulus
    ):
        raise ValueError("invalid holdout modulus/remainder")
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
    bounds = policy.bounds
    statistics = _artifact_statistics(connection, bounds)
    failures = _check_build_statistics(statistics, policy)
    if failures:
        raise ValueError("quality gate failed: " + "; ".join(failures))
    return statistics


def _observation_key(observation: Observation) -> bytes:
    return (
        f"{observation.cep}|{observation.point.latitude:.7f}|"
        f"{observation.point.longitude:.7f}|{observation.point.source}"
    ).encode("utf-8")


def split_holdout(
    observations: Iterable[Observation], policy: QualityPolicy
) -> tuple[list[Observation], list[Observation]]:
    modulus = int(policy.holdout["modulus"])
    remainder = int(policy.holdout["remainder"])
    training: list[Observation] = []
    heldout: list[Observation] = []
    for observation in observations:
        bucket = (
            int.from_bytes(
                hashlib.sha256(_observation_key(observation)).digest()[:8], "big"
            )
            % modulus
        )
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
    for precision in (
        "observed_cep",
        "osm_postcode",
        "observed_cep_prefix",
        "municipality",
    ):
        groups[f"precision:{precision}"]
    return {key: _percentiles(groups[key]) for key in sorted(groups)}


def _report_checks(
    artifact: Mapping[str, object],
    metrics: Mapping[str, Mapping[str, object]],
    heldout_count: int,
    holdout_ufs: set[str],
    address_classes: set[str],
    official_metrics: Mapping[str, Mapping[str, object]],
    official_count: int,
    policy: QualityPolicy,
) -> tuple[list[dict[str, object]], list[str]]:
    checks: list[dict[str, object]] = []

    def add(name: str, passed: bool, actual: object, threshold: object) -> None:
        checks.append(
            {
                "name": name,
                "passed": passed,
                "actual": actual,
                "threshold": threshold,
            }
        )

    build_failures = _check_build_statistics(artifact, policy)
    add("artifact_build_policy", not build_failures, build_failures, [])
    holdout = policy.holdout
    add(
        "holdout_record_count",
        heldout_count >= holdout["minimum_records"],
        heldout_count,
        holdout["minimum_records"],
    )
    add(
        "official_holdout_record_count",
        official_count >= holdout["minimum_official_records"],
        official_count,
        holdout["minimum_official_records"],
    )
    add(
        "holdout_uf_coverage",
        len(holdout_ufs) >= holdout["minimum_ufs"],
        len(holdout_ufs),
        holdout["minimum_ufs"],
    )
    required_classes = set(holdout["required_address_classes"])
    add(
        "holdout_address_classes",
        required_classes <= address_classes,
        sorted(address_classes),
        sorted(required_classes),
    )
    thresholds = policy.error_thresholds
    mappings = (
        ("overall_p95", "overall"),
        ("osm_postcode_p95", "precision:osm_postcode"),
        ("municipality_p95", "precision:municipality"),
    )
    for threshold_name, metric_name in mappings:
        actual = metrics.get(metric_name, {}).get("p95_km")
        ceiling = thresholds[threshold_name]
        add(
            threshold_name,
            isinstance(actual, (int, float)) and actual <= ceiling,
            actual,
            ceiling,
        )
    official_actual = official_metrics.get("overall", {}).get("p95_km")
    official_ceiling = thresholds["official_overall_p95"]
    add(
        "official_overall_p95",
        isinstance(official_actual, (int, float))
        and official_actual <= official_ceiling,
        official_actual,
        official_ceiling,
    )
    failures = [check["name"] for check in checks if not check["passed"]]
    return checks, failures


def build_quality_report(
    *,
    database_path: str | Path,
    ibge_path: str | Path,
    osm_observations_path: str | Path,
    official_holdout_path: str | Path,
    enrichment_config_path: str | Path,
    quality_policy_path: str | Path,
) -> dict[str, object]:
    policy = load_quality_policy(quality_policy_path)
    enrichment, enrichment_record = load_enrichment_config(enrichment_config_path)
    database = Path(database_path)
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        artifact = _artifact_statistics(connection, policy.bounds)
        dataset_version_row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'dataset_version'"
        ).fetchone()
        if dataset_version_row is None:
            raise ValueError("database has no dataset_version metadata")
        dataset_version = dataset_version_row[0]

        osm = load_osm_observations(osm_observations_path)
        training, heldout = split_holdout(osm, policy)
        municipalities = load_ibge_municipality_references(ibge_path)
        estimator = CentroidEstimator(
            (),
            municipalities,
            osm_observations=training,
            min_prefix_samples=enrichment.min_prefix_samples,
            max_prefix_radius_km=enrichment.max_prefix_radius_km,
            max_observed_radius_km=enrichment.max_observed_radius_km,
            max_osm_radius_km=enrichment.max_osm_radius_km,
            outlier_min_samples=enrichment.outlier_min_samples,
            outlier_mad_multiplier=enrichment.outlier_mad_multiplier,
            outlier_floor_km=enrichment.outlier_floor_km,
        )
        heldout_ceps = {observation.cep for observation in heldout}
        official = load_observations(official_holdout_path)
        metadata_ceps = heldout_ceps | {observation.cep for observation in official}
        metadata: dict[
            str,
            tuple[
                str,
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
            SELECT cep, ibge, uf, city, street, neighborhood,
                   latitude, longitude, precision
              FROM cep_geo
            """
        ):
            if row["cep"] in metadata_ceps:
                metadata[row["cep"]] = (
                    row["ibge"],
                    row["uf"],
                    row["city"],
                    row["street"],
                    row["neighborhood"],
                    row["latitude"],
                    row["longitude"],
                    row["precision"],
                )

        samples: list[tuple[str, str, str, float]] = []
        missing_ceps = 0
        prediction_failures = 0
        for observation in heldout:
            row = metadata.get(observation.cep)
            if row is None:
                missing_ceps += 1
                continue
            ibge, uf, _city, street, neighborhood, _lat, _lon, _precision = row
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

        official_samples: list[tuple[str, str, str, float]] = []
        official_missing_ceps = 0
        official_prediction_failures = 0
        for observation in official:
            row = metadata.get(observation.cep)
            if row is None:
                official_missing_ceps += 1
                continue
            _ibge, uf, _city, street, neighborhood, latitude, longitude, precision = row
            if latitude is None or longitude is None or precision is None:
                official_prediction_failures += 1
                continue
            address_class = (
                "urban_address_proxy"
                if street or neighborhood
                else "rural_or_general_address_proxy"
            )
            error = haversine_km(
                Point(latitude, longitude, "prediction"), observation.point
            )
            official_samples.append((uf, address_class, precision, error))

        metrics = _group_metrics(samples)
        official_metrics = _group_metrics(official_samples)
        holdout_ufs = {sample[0] for sample in samples}
        address_classes = {sample[1] for sample in samples}
        checks, failures = _report_checks(
            artifact,
            metrics,
            len(samples),
            holdout_ufs,
            address_classes,
            official_metrics,
            len(official_samples),
            policy,
        )
        missing_ufs = sorted(set(_IBGE_UF.values()) - holdout_ufs)
        return {
            "format": _REPORT_FORMAT,
            "quality_version": policy.version,
            "dataset_version": dataset_version,
            "status": "pass" if not failures else "fail",
            "failures": failures,
            "inputs": {
                "database_sha256": _file_sha256(database),
                "osm_observations_sha256": _file_sha256(Path(osm_observations_path)),
                "official_holdout_sha256": _file_sha256(Path(official_holdout_path)),
                "quality_policy_sha256": policy.sha256,
                "enrichment_config_sha256": enrichment_record["sha256"],
            },
            "artifact": artifact,
            "holdout": {
                "algorithm": policy.holdout["algorithm"],
                "modulus": policy.holdout["modulus"],
                "remainder": policy.holdout["remainder"],
                "training_observations": len(training),
                "heldout_observations": len(heldout),
                "evaluated_observations": len(samples),
                "missing_ceps": missing_ceps,
                "prediction_failures": prediction_failures,
                "ufs": sorted(holdout_ufs),
                "missing_ufs": missing_ufs,
                "address_classes": sorted(address_classes),
                "leakage_control": "held-out OSM observations are excluded from every evaluated centroid",
            },
            "metrics": metrics,
            "official_holdout": {
                "source": "SEFAZ-BA/PRODEB Preço da Hora public API samples",
                "evaluated_observations": len(official_samples),
                "missing_ceps": official_missing_ceps,
                "prediction_failures": official_prediction_failures,
                "ufs": sorted({sample[0] for sample in official_samples}),
                "metrics": official_metrics,
                "independence": "coordinates were not supplied to the OpenCEPGeo builder",
                "distribution": "internal validation summary only; source terms do not establish dataset redistribution rights",
            },
            "checks": checks,
            "evidence_gaps": [
                "The official pilot holdout covers Salvador/BA only; it is not nationally representative.",
                "OSM explicit-postcode nodes are independent of OpenCEP and IBGE, but remain community-mapped evidence.",
                "Urban/rural is an address-metadata proxy, not an official territorial classification.",
                "No observed_cep or observed_cep_prefix error sample exists because no approved production first-party corpus was used to build this release.",
                *(
                    [f"No held-out observation for UF(s): {', '.join(missing_ufs)}"]
                    if missing_ufs
                    else []
                ),
            ],
        }
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
    holdout = report["holdout"]
    metrics = report["metrics"]
    official = report["official_holdout"]
    assert isinstance(artifact, dict)
    assert isinstance(holdout, dict)
    assert isinstance(metrics, dict)
    assert isinstance(official, dict)
    lines = [
        f"# OpenCEPGeo quality report {report['quality_version']}",
        "",
        f"Status: **{str(report['status']).upper()}**",
        "",
        "## Artifact coverage",
        "",
        f"- Records: {artifact['record_count']}",
        f"- Located: {artifact['located']} ({artifact['coverage']:.6%})",
        f"- Unresolved: {artifact['unresolved']}",
        f"- UFs: {artifact['uf_count']}/27",
        f"- Tier distribution: `{json.dumps(artifact['tier_distribution'], sort_keys=True)}`",
        "",
        "## Leakage-controlled holdout",
        "",
        f"- Training observations: {holdout['training_observations']}",
        f"- Held-out observations: {holdout['heldout_observations']}",
        f"- Evaluated observations: {holdout['evaluated_observations']}",
        f"- UFs: {len(holdout['ufs'])}/27",
        "",
        "| Group | Count | p50 km | p90 km | p95 km |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
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
    lines.extend(
        [
            "",
            "## Independent official pilot",
            "",
            f"- Evaluated observations: {official['evaluated_observations']}",
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
