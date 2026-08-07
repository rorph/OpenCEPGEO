from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from statistics import median

from .model import GeoEstimate, MunicipalityReference, Observation, Point

_NON_DIGIT = re.compile(r"\D")
_EARTH_RADIUS_KM = 6371.0088


def normalize_cep(value: object) -> str | None:
    digits = _NON_DIGIT.sub("", str(value or ""))
    return digits if len(digits) == 8 else None


def normalize_ibge(value: object) -> str | None:
    digits = _NON_DIGIT.sub("", str(value or ""))
    return digits if len(digits) == 7 else None


def haversine_km(a: Point, b: Point) -> float:
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    dlat = lat2 - lat1
    dlon = math.radians(b.longitude - a.longitude)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def robust_centroid(
    points: Iterable[Point], precision: str, method: str
) -> GeoEstimate:
    samples = tuple(points)
    if not samples:
        raise ValueError("at least one point is required")
    latitude = median(point.latitude for point in samples)
    longitude = median(point.longitude for point in samples)
    center = Point(latitude, longitude, "centroid")
    radius = max(haversine_km(center, point) for point in samples)
    return GeoEstimate(
        latitude=latitude,
        longitude=longitude,
        precision=precision,
        method=method,
        evidence_count=len(samples),
        uncertainty_km=round(radius, 3),
        sources=tuple(sorted({point.source for point in samples})),
    )


def reject_outliers(
    points: Iterable[Point],
    *,
    minimum_samples: int,
    mad_multiplier: float,
    floor_km: float,
) -> tuple[Point, ...]:
    samples = tuple(points)
    if len(samples) < minimum_samples:
        return samples
    center = Point(
        median(point.latitude for point in samples),
        median(point.longitude for point in samples),
        "outlier-filter-center",
    )
    distances = tuple(haversine_km(center, point) for point in samples)
    median_distance = median(distances)
    mad = median(abs(distance - median_distance) for distance in distances)
    cutoff = max(floor_km, median_distance + mad_multiplier * mad)
    return tuple(
        point for point, distance in zip(samples, distances) if distance <= cutoff
    )


class CentroidEstimator:
    """Resolve a CEP using observed points, safe prefix groups, then IBGE."""

    def __init__(
        self,
        observations: Iterable[Observation],
        municipality_points: Mapping[str, Point | MunicipalityReference],
        *,
        osm_observations: Iterable[Observation] = (),
        min_prefix_samples: int = 3,
        max_prefix_radius_km: float = 25.0,
        max_observed_radius_km: float = 10.0,
        max_osm_radius_km: float = 5.0,
        outlier_min_samples: int = 3,
        outlier_mad_multiplier: float = 3.0,
        outlier_floor_km: float = 0.25,
    ) -> None:
        if min_prefix_samples < 2:
            raise ValueError("min_prefix_samples must be at least 2")
        if max_prefix_radius_km <= 0:
            raise ValueError("max_prefix_radius_km must be positive")
        if max_observed_radius_km <= 0 or max_osm_radius_km <= 0:
            raise ValueError("exact-tier radii must be positive")
        if outlier_min_samples < 3:
            raise ValueError("outlier_min_samples must be at least 3")
        if outlier_mad_multiplier <= 0 or outlier_floor_km <= 0:
            raise ValueError("outlier thresholds must be positive")

        exact: dict[str, list[Point]] = defaultdict(list)
        prefix: dict[tuple[str, str], list[Point]] = defaultdict(list)
        for observation in observations:
            cep = normalize_cep(observation.cep)
            if cep is None:
                continue
            exact[cep].append(observation.point)
            ibge = normalize_ibge(observation.ibge)
            if ibge is not None:
                prefix[(cep[:5], ibge)].append(observation.point)

        self._exact = dict(exact)
        self._prefix = dict(prefix)
        osm_exact: dict[str, list[Point]] = defaultdict(list)
        for observation in osm_observations:
            cep = normalize_cep(observation.cep)
            if cep is not None:
                osm_exact[cep].append(observation.point)
        self._osm_exact = dict(osm_exact)
        self._municipalities = {
            code: (
                reference
                if isinstance(reference, MunicipalityReference)
                else MunicipalityReference(reference, 1, 0.0)
            )
            for raw_code, reference in municipality_points.items()
            if (code := normalize_ibge(raw_code)) is not None
        }
        self._min_prefix_samples = min_prefix_samples
        self._max_prefix_radius_km = max_prefix_radius_km
        self._max_observed_radius_km = max_observed_radius_km
        self._max_osm_radius_km = max_osm_radius_km
        self._outlier_min_samples = outlier_min_samples
        self._outlier_mad_multiplier = outlier_mad_multiplier
        self._outlier_floor_km = outlier_floor_km

    def _bounded_centroid(
        self,
        points: Iterable[Point],
        *,
        precision: str,
        method: str,
        max_radius_km: float,
        min_samples: int = 1,
    ) -> GeoEstimate | None:
        samples = reject_outliers(
            points,
            minimum_samples=self._outlier_min_samples,
            mad_multiplier=self._outlier_mad_multiplier,
            floor_km=self._outlier_floor_km,
        )
        if len(samples) < min_samples:
            return None
        estimate = robust_centroid(samples, precision, method)
        return estimate if estimate.uncertainty_km <= max_radius_km else None

    def estimate(self, cep: object, ibge: object = None) -> GeoEstimate | None:
        cep8 = normalize_cep(cep)
        if cep8 is None:
            return None

        exact_points = self._exact.get(cep8)
        if exact_points:
            estimate = self._bounded_centroid(
                exact_points,
                precision="observed_cep",
                method="robust_median_first_party",
                max_radius_km=self._max_observed_radius_km,
            )
            if estimate is not None:
                return estimate

        osm_points = self._osm_exact.get(cep8)
        if osm_points:
            estimate = self._bounded_centroid(
                osm_points,
                precision="osm_postcode",
                method="robust_median_osm_postcode",
                max_radius_km=self._max_osm_radius_km,
            )
            if estimate is not None:
                return estimate

        ibge7 = normalize_ibge(ibge)
        if ibge7 is not None:
            prefix_points = self._prefix.get((cep8[:5], ibge7), ())
            estimate = self._bounded_centroid(
                prefix_points,
                precision="observed_cep_prefix",
                method="bounded_same_ibge_prefix_median",
                max_radius_km=self._max_prefix_radius_km,
                min_samples=self._min_prefix_samples,
            )
            if estimate is not None:
                return estimate

            municipality = self._municipalities.get(ibge7)
            if municipality is not None:
                return GeoEstimate(
                    latitude=municipality.point.latitude,
                    longitude=municipality.point.longitude,
                    precision="municipality",
                    method="ibge_city_reference_with_locality_dispersion",
                    evidence_count=municipality.evidence_count,
                    uncertainty_km=municipality.uncertainty_km,
                    sources=(municipality.point.source,),
                )

        return None
