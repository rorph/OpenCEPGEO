from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .estimator import haversine_km, normalize_cep, normalize_ibge
from .model import MunicipalityReference, Observation, Point


@dataclass(frozen=True)
class OpenCEPEntry:
    member: str
    sha256: str
    record: dict[str, object]


def _decode_json(payload: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid OpenCEP JSON in {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OpenCEP record must be an object: {name}")
    return value


def iter_opencep_entries(path: str | Path) -> Iterator[OpenCEPEntry]:
    """Stream OpenCEP JSON entries with immutable member provenance."""
    source = Path(path)
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for member in sorted(archive.infolist(), key=lambda item: item.filename):
                if member.is_dir() or not member.filename.lower().endswith(".json"):
                    continue
                payload = archive.read(member)
                yield OpenCEPEntry(
                    member=member.filename,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    record=_decode_json(payload, member.filename),
                )
        return

    if source.is_dir():
        for filename in sorted(source.rglob("*.json")):
            payload = filename.read_bytes()
            yield OpenCEPEntry(
                member=filename.relative_to(source).as_posix(),
                sha256=hashlib.sha256(payload).hexdigest(),
                record=_decode_json(payload, str(filename)),
            )
        return

    raise ValueError(f"OpenCEP input is not a ZIP or directory: {source}")


def _load_corrections(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    correction_path = Path(path)
    try:
        document = json.loads(correction_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"cannot read OpenCEP corrections {correction_path}: {exc}"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != "opencepgeo-corrections-v1"
    ):
        raise ValueError("unsupported or missing OpenCEP corrections format")
    raw_corrections = document.get("corrections")
    if not isinstance(raw_corrections, list) or not raw_corrections:
        raise ValueError("OpenCEP corrections must be a non-empty list")
    corrections: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(raw_corrections):
        if not isinstance(raw, dict):
            raise ValueError(f"corrections[{index}] must be an object")
        required = ("member", "source_sha256", "field", "from", "to", "reason")
        if any(
            not isinstance(raw.get(field), str) or not raw[field] for field in required
        ):
            raise ValueError(f"corrections[{index}] has missing metadata")
        member = raw["member"]
        if member in corrections:
            raise ValueError(f"duplicate correction member: {member}")
        if len(raw["source_sha256"]) != 64:
            raise ValueError(f"invalid correction SHA-256 for {member}")
        corrections[member] = {field: raw[field] for field in required}
    return corrections


def iter_opencep_records(
    path: str | Path,
    corrections_path: str | Path | None = None,
) -> Iterator[dict[str, object]]:
    """Stream records and apply only checksum-bound, audited corrections."""
    corrections = _load_corrections(corrections_path)
    applied: set[str] = set()
    for entry in iter_opencep_entries(path):
        record = entry.record
        correction = corrections.get(entry.member)
        if correction is not None:
            if entry.sha256 != correction["source_sha256"]:
                raise ValueError(f"correction source checksum changed: {entry.member}")
            field = correction["field"]
            if record.get(field) != correction["from"]:
                raise ValueError(
                    f"correction source value changed: {entry.member}.{field}"
                )
            record = dict(record)
            record[field] = correction["to"]
            applied.add(entry.member)

        member_stem = Path(entry.member).stem
        if len(member_stem) == 8 and member_stem.isdigit():
            cep = normalize_cep(record.get("cep"))
            if cep != member_stem:
                raise ValueError(
                    f"OpenCEP member/CEP mismatch: {entry.member} contains {record.get('cep')!r}"
                )
        yield record

    unused = sorted(set(corrections) - applied)
    if unused:
        raise ValueError(f"unused OpenCEP correction(s): {', '.join(unused)}")


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
                raise ValueError(
                    f"invalid observation at row {row_number}: {exc}"
                ) from exc
            observations.append(
                Observation(cep=cep, ibge=normalize_ibge(row.get("ibge")), point=point)
            )
    return observations


def load_osm_observations(path: str | Path | None) -> list[Observation]:
    observations = load_observations(path)
    for observation in observations:
        if not observation.point.source.startswith("openstreetmap:"):
            raise ValueError(
                f"OSM evidence has invalid source ID: {observation.point.source}"
            )
        if observation.ibge is not None:
            raise ValueError("OSM postcode evidence must not assert an IBGE code")
    return observations


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _ibge_geopackage(path: str | Path) -> Iterator[Path]:
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"IBGE input is not a file: {source}")
    if not zipfile.is_zipfile(source):
        yield source
        return

    with zipfile.ZipFile(source) as archive:
        members = [
            member
            for member in archive.infolist()
            if not member.is_dir() and member.filename.lower().endswith(".gpkg")
        ]
        if len(members) != 1:
            raise ValueError("IBGE ZIP must contain exactly one GeoPackage")
        descriptor, temporary_name = tempfile.mkstemp(suffix=".gpkg")
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with (
                archive.open(members[0]) as input_handle,
                temporary.open("wb") as output,
            ):
                shutil.copyfileobj(input_handle, output, length=1024 * 1024)
            yield temporary
        finally:
            temporary.unlink(missing_ok=True)


def load_ibge_municipality_references(
    path: str | Path,
) -> dict[str, MunicipalityReference]:
    """Read municipality reference points directly from the IBGE GeoPackage.

    Localidades do Brasil stores latitude and longitude as ordinary attributes,
    so no GIS library is needed.
    """
    with _ibge_geopackage(path) as geopackage:
        connection = sqlite3.connect(f"file:{geopackage.resolve()}?mode=ro", uri=True)
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
                SELECT CD_MUN, CT_LOCALIDADE, LAT_LOCALIDADE, LONG_LOCALIDADE
                  FROM {quoted}
                 WHERE LAT_LOCALIDADE IS NOT NULL
                   AND LONG_LOCALIDADE IS NOT NULL
            """
            locality_points: dict[str, list[tuple[str, Point]]] = defaultdict(list)
            for raw_ibge, category, latitude, longitude in connection.execute(query):
                ibge = normalize_ibge(raw_ibge)
                if ibge is None:
                    raise ValueError(
                        f"invalid IBGE municipality code in GeoPackage: {raw_ibge!r}"
                    )
                locality_points[ibge].append(
                    (
                        str(category),
                        Point(
                            latitude=float(latitude),
                            longitude=float(longitude),
                            source="ibge-localidades",
                        ),
                    )
                )
            references: dict[str, MunicipalityReference] = {}
            for ibge, localities in locality_points.items():
                cities = [
                    point for category, point in localities if category == "Cidade"
                ]
                if not cities:
                    continue
                city = Point(
                    latitude=sum(point.latitude for point in cities) / len(cities),
                    longitude=sum(point.longitude for point in cities) / len(cities),
                    source="ibge-localidades",
                )
                references[ibge] = MunicipalityReference(
                    point=city,
                    evidence_count=len(localities),
                    uncertainty_km=round(
                        max(haversine_km(city, point) for _, point in localities), 3
                    ),
                )
            if not references:
                raise ValueError("IBGE GeoPackage produced no municipality points")
            return references
        finally:
            connection.close()


def load_ibge_municipality_points(path: str | Path) -> dict[str, Point]:
    return {
        ibge: reference.point
        for ibge, reference in load_ibge_municipality_references(path).items()
    }
