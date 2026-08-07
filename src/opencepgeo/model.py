from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Point:
    latitude: float
    longitude: float
    source: str

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError(f"invalid latitude: {self.latitude}")
        if not -180 <= self.longitude <= 180:
            raise ValueError(f"invalid longitude: {self.longitude}")
        if not self.source.strip():
            raise ValueError("point source must not be empty")


@dataclass(frozen=True, slots=True)
class Observation:
    cep: str
    point: Point
    ibge: str | None = None


@dataclass(frozen=True, slots=True)
class GeoEstimate:
    latitude: float
    longitude: float
    precision: str
    sample_size: int
    radius_km: float | None
    sources: tuple[str, ...]

    def as_geojson(self) -> dict[str, object]:
        return {
            "type": "Point",
            "coordinates": [self.longitude, self.latitude],
            "precision": self.precision,
            "sample_size": self.sample_size,
            "radius_km": self.radius_km,
            "source": list(self.sources),
        }

