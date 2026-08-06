from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from statistics import median

from .model import GeoEstimate, Observation, Point

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


def robust_centroid(points: Iterable[Point], precision: str) -> GeoEstimate:
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
        sample_size=len(samples),
        radius_km=round(radius, 3),
        sources=tuple(sorted({point.source for point in samples})),
    )


class CentroidEstimator:
    """Resolve a CEP using observed points, safe prefix groups, then IBGE."""

    def __init__(
        self,
        observations: Iterable[Observation],
        municipality_points: Mapping[str, Point],
        *,
        min_prefix_samples: int = 3,
        max_prefix_radius_km: float = 25.0,
    ) -> None:
        if min_prefix_samples < 2:
            raise ValueError("min_prefix_samples must be at least 2")
        if max_prefix_radius_km <= 0:
            raise ValueError("max_prefix_radius_km must be positive")

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
        self._municipalities = {
            code: point
            for raw_code, point in municipality_points.items()
            if (code := normalize_ibge(raw_code)) is not None
        }
        self._min_prefix_samples = min_prefix_samples
        self._max_prefix_radius_km = max_prefix_radius_km

    def estimate(self, cep: object, ibge: object = None) -> GeoEstimate | None:
        cep8 = normalize_cep(cep)
        if cep8 is None:
            return None

        exact_points = self._exact.get(cep8)
        if exact_points:
            return robust_centroid(exact_points, "observed_cep")

        ibge7 = normalize_ibge(ibge)
        if ibge7 is not None:
            prefix_points = self._prefix.get((cep8[:5], ibge7), ())
            if len(prefix_points) >= self._min_prefix_samples:
                estimate = robust_centroid(prefix_points, "observed_cep_prefix")
                if (
                    estimate.radius_km is not None
                    and estimate.radius_km <= self._max_prefix_radius_km
                ):
                    return estimate

            municipality = self._municipalities.get(ibge7)
            if municipality is not None:
                return GeoEstimate(
                    latitude=municipality.latitude,
                    longitude=municipality.longitude,
                    precision="municipality",
                    sample_size=1,
                    radius_km=None,
                    sources=(municipality.source,),
                )

        return None

