"""Offline CEP centroid estimation with explicit precision metadata."""

from .estimator import CentroidEstimator, normalize_cep, normalize_ibge
from .model import GeoEstimate, MunicipalityReference, Observation, Point

__all__ = [
    "CentroidEstimator",
    "GeoEstimate",
    "MunicipalityReference",
    "Observation",
    "Point",
    "normalize_cep",
    "normalize_ibge",
]

__version__ = "0.1.0"
