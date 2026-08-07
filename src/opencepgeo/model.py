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
class MunicipalityReference:
    point: Point
    evidence_count: int
    uncertainty_km: float

    def __post_init__(self) -> None:
        if self.evidence_count < 1:
            raise ValueError("municipality evidence_count must be positive")
        if self.uncertainty_km < 0:
            raise ValueError("municipality uncertainty_km must not be negative")


@dataclass(frozen=True, slots=True)
class GeoEstimate:
    latitude: float
    longitude: float
    precision: str
    method: str
    evidence_count: int
    uncertainty_km: float
    sources: tuple[str, ...]

    @property
    def sample_size(self) -> int:
        return self.evidence_count

    @property
    def radius_km(self) -> float:
        return self.uncertainty_km

    def as_geojson(self) -> dict[str, object]:
        return {
            "type": "Point",
            "coordinates": [self.longitude, self.latitude],
            "precision": self.precision,
            "method": self.method,
            "evidence_count": self.evidence_count,
            "uncertainty_km": self.uncertainty_km,
            "source": list(self.sources),
        }
