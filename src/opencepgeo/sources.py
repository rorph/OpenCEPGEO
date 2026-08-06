from __future__ import annotations

import csv
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from pathlib import Path

from .estimator import normalize_cep, normalize_ibge
from .model import Observation, Point


def _decode_json(payload: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid OpenCEP JSON in {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OpenCEP record must be an object: {name}")
    return value


def iter_opencep_records(path: str | Path) -> Iterator[dict[str, object]]:
    """Stream OpenCEP JSON records from its release ZIP or an extracted tree."""
    source = Path(path)
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                if member.is_dir() or not member.filename.lower().endswith(".json"):
                    continue
                yield _decode_json(archive.read(member), member.filename)
        return

    if source.is_dir():
        for filename in sorted(source.rglob("*.json")):
            yield _decode_json(filename.read_bytes(), str(filename))
        return

    raise ValueError(f"OpenCEP input is not a ZIP or directory: {source}")


def load_observations(path: str | Path | None) -> list[Observation]:
    if path is None:
        return []
    observations: list[Observation] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"cep", "latitude", "longitude", "source"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"observations CSV requires columns: {sorted(required)}")
        for row_number, row in enumerate(reader, start=2):
            cep = normalize_cep(row.get("cep"))
            if cep is None:
                raise ValueError(f"invalid CEP at observations row {row_number}")
            try:
                point = Point(
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    source=row["source"].strip(),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid observation at row {row_number}: {exc}") from exc
            observations.append(
                Observation(cep=cep, ibge=normalize_ibge(row.get("ibge")), point=point)
            )
    return observations


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def load_ibge_municipality_points(path: str | Path) -> dict[str, Point]:
    """Read municipality reference points directly from the IBGE GeoPackage.

    Localidades do Brasil stores latitude and longitude as ordinary attributes,
    so no GIS library is needed.
    """
    connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'"
            )
        ]
        required = {
            "CD_MUN",
            "CT_LOCALIDADE",
            "LAT_LOCALIDADE",
            "LONG_LOCALIDADE",
        }
        table = None
        for candidate in tables:
            columns = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA table_info({_quote_identifier(candidate)})"
                )
            }
            if required.issubset(columns):
                table = candidate
                break
        if table is None:
            raise ValueError("IBGE GeoPackage has no compatible Localidades layer")

        quoted = _quote_identifier(table)
        query = f"""
            SELECT CD_MUN, AVG(LAT_LOCALIDADE), AVG(LONG_LOCALIDADE)
              FROM {quoted}
             WHERE CT_LOCALIDADE = 'Cidade'
               AND LAT_LOCALIDADE IS NOT NULL
               AND LONG_LOCALIDADE IS NOT NULL
             GROUP BY CD_MUN
        """
        points: dict[str, Point] = {}
        for raw_ibge, latitude, longitude in connection.execute(query):
            ibge = normalize_ibge(raw_ibge)
            if ibge is not None:
                points[ibge] = Point(
                    latitude=float(latitude),
                    longitude=float(longitude),
                    source="ibge-localidades",
                )
        if not points:
            raise ValueError("IBGE GeoPackage produced no municipality points")
        return points
    finally:
        connection.close()

