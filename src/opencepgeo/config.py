from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EnrichmentConfig:
    version: str
    min_prefix_samples: int
    max_prefix_radius_km: float
    max_observed_radius_km: float
    max_osm_radius_km: float
    max_osm_municipality_distance_km: float
    outlier_min_samples: int
    outlier_mad_multiplier: float
    outlier_floor_km: float

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("enrichment config version must not be empty")
        if self.min_prefix_samples < 3:
            raise ValueError("min_prefix_samples must be at least 3")
        if self.outlier_min_samples < 3:
            raise ValueError("outlier_min_samples must be at least 3")
        positive = (
            self.max_prefix_radius_km,
            self.max_observed_radius_km,
            self.max_osm_radius_km,
            self.max_osm_municipality_distance_km,
            self.outlier_mad_multiplier,
            self.outlier_floor_km,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("enrichment thresholds must be positive")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return float(value)


def load_enrichment_config(
    path: str | Path,
) -> tuple[EnrichmentConfig, dict[str, object]]:
    config_path = Path(path)
    payload = config_path.read_bytes()
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid enrichment config {config_path}: {exc}") from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != "opencepgeo-enrichment-v1"
    ):
        raise ValueError("unsupported or missing enrichment config format")
    thresholds = document.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("enrichment config thresholds must be an object")
    config = EnrichmentConfig(
        version=str(document.get("version") or ""),
        min_prefix_samples=_integer(
            thresholds.get("min_prefix_samples"), "min_prefix_samples"
        ),
        max_prefix_radius_km=_number(
            thresholds.get("max_prefix_radius_km"), "max_prefix_radius_km"
        ),
        max_observed_radius_km=_number(
            thresholds.get("max_observed_radius_km"), "max_observed_radius_km"
        ),
        max_osm_radius_km=_number(
            thresholds.get("max_osm_radius_km"), "max_osm_radius_km"
        ),
        max_osm_municipality_distance_km=_number(
            thresholds.get("max_osm_municipality_distance_km"),
            "max_osm_municipality_distance_km",
        ),
        outlier_min_samples=_integer(
            thresholds.get("outlier_min_samples"), "outlier_min_samples"
        ),
        outlier_mad_multiplier=_number(
            thresholds.get("outlier_mad_multiplier"), "outlier_mad_multiplier"
        ),
        outlier_floor_km=_number(
            thresholds.get("outlier_floor_km"), "outlier_floor_km"
        ),
    )
    return config, {
        "filename": config_path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "content": document,
    }
