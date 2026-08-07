from __future__ import annotations

import re
from dataclasses import dataclass


_EVIDENCE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Point:
    latitude: float
    longitude: float
    source: str
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError(f"invalid latitude: {self.latitude}")
        if not -180 <= self.longitude <= 180:
            raise ValueError(f"invalid longitude: {self.longitude}")
        if not self.source.strip():
            raise ValueError("point source must not be empty")
        if self.evidence_id is not None and not self.evidence_id.strip():
            raise ValueError("point evidence_id must not be empty")


@dataclass(frozen=True, slots=True)
class Observation:
    cep: str
    point: Point
    ibge: str | None = None


@dataclass(frozen=True, slots=True)
class MunicipalityReference:
    point: Point
    evidence_count: int
    evidence_radius_km: float
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_count < 1:
            raise ValueError("municipality evidence_count must be positive")
        if self.evidence_radius_km < 0:
            raise ValueError("municipality evidence_radius_km must not be negative")
        if not _EVIDENCE_DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("municipality evidence_digest must be SHA-256")


@dataclass(frozen=True, slots=True)
class GeoEstimate:
    latitude: float
    longitude: float
    precision: str
    method: str
    evidence_count: int
    evidence_radius_km: float
    sources: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_count < 1:
            raise ValueError("evidence_count must be positive")
        if self.evidence_radius_km < 0:
            raise ValueError("evidence_radius_km must not be negative")
        if not self.sources or len(self.sources) > 16:
            raise ValueError("source categories must contain 1-16 entries")
        if any(
            not source or len(source.encode("utf-8")) > 64 for source in self.sources
        ):
            raise ValueError("source category must contain 1-64 UTF-8 bytes")
        if tuple(sorted(set(self.sources))) != self.sources:
            raise ValueError("source categories must be sorted and unique")
        if not _EVIDENCE_DIGEST.fullmatch(self.evidence_digest):
            raise ValueError("evidence_digest must be SHA-256")

    @property
    def sample_size(self) -> int:
        return self.evidence_count

    @property
    def radius_km(self) -> float:
        return self.evidence_radius_km

    def as_geojson(self) -> dict[str, object]:
        return {
            "type": "Point",
            "coordinates": [self.longitude, self.latitude],
            "precision": self.precision,
            "method": self.method,
            "evidence_count": self.evidence_count,
            "evidence_radius_km": self.evidence_radius_km,
            "source": list(self.sources),
            "evidence_digest": self.evidence_digest,
        }
