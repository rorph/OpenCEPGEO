"""Offline CEP centroid estimation with explicit precision metadata."""

from .estimator import CentroidEstimator, normalize_cep, normalize_ibge
from .model import GeoEstimate, Observation, Point

__all__ = [
    "CentroidEstimator",
    "GeoEstimate",
    "Observation",
    "Point",
    "normalize_cep",
    "normalize_ibge",
]

__version__ = "0.1.0"

