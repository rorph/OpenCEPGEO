from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import zlib
from collections.abc import Iterator
from pathlib import Path

from .estimator import normalize_cep
from .model import Point
from .source_lock import load_source_lock, verify_file


class PBFError(ValueError):
    """The local OSM PBF is malformed or uses an unsupported encoding."""


def _varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(data) and shift < 70:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
    raise PBFError("invalid protobuf varint")


def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _int64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _fields(data: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    position = 0
    while position < len(data):
        key, position = _varint(data, position)
        field_number = key >> 3
        wire_type = key & 7
        if field_number == 0:
            raise PBFError("protobuf field number must not be zero")
        if wire_type == 0:
            value, position = _varint(data, position)
            yield field_number, wire_type, value
        elif wire_type == 1:
            end = position + 8
            if end > len(data):
                raise PBFError("truncated protobuf fixed64")
            yield field_number, wire_type, data[position:end]
            position = end
        elif wire_type == 2:
            length, position = _varint(data, position)
            end = position + length
            if end > len(data):
                raise PBFError("truncated protobuf bytes")
            yield field_number, wire_type, data[position:end]
            position = end
        elif wire_type == 5:
            end = position + 4
            if end > len(data):
                raise PBFError("truncated protobuf fixed32")
            yield field_number, wire_type, data[position:end]
            position = end
        else:
            raise PBFError(f"unsupported protobuf wire type: {wire_type}")


def _packed(data: bytes, *, zigzag: bool = False, delta: bool = False) -> list[int]:
    values: list[int] = []
    position = 0
    previous = 0
    while position < len(data):
        value, position = _varint(data, position)
        if zigzag:
            value = _zigzag(value)
        if delta:
            previous += value
            value = previous
        values.append(value)
    return values


def _field_values(message: bytes, field_number: int) -> list[int | bytes]:
    return [value for number, _, value in _fields(message) if number == field_number]


def _scalar(message: bytes, field_number: int, default: int = 0) -> int:
    values = _field_values(message, field_number)
    if not values:
        return default
    value = values[-1]
    if not isinstance(value, int):
        raise PBFError(f"protobuf field {field_number} is not scalar")
    return value


def _string_table(message: bytes) -> list[str]:
    payloads = _field_values(message, 1)
    if not payloads or any(not isinstance(value, bytes) for value in payloads):
        raise PBFError("OSM PBF string table is missing")
    try:
        return [value.decode("utf-8") for value in payloads if isinstance(value, bytes)]
    except UnicodeDecodeError as exc:
        raise PBFError(f"invalid UTF-8 in OSM string table: {exc}") from exc


def _tags(keys: list[int], values: list[int], strings: list[str]) -> dict[str, str]:
    if len(keys) != len(values):
        raise PBFError("OSM node key/value counts differ")
    try:
        return {strings[key]: strings[value] for key, value in zip(keys, values)}
    except IndexError as exc:
        raise PBFError("OSM node tag index is outside the string table") from exc


def _node(
    message: bytes,
    strings: list[str],
    granularity: int,
    lat_offset: int,
    lon_offset: int,
):
    node_id = _zigzag(_scalar(message, 1))
    key_payloads = _field_values(message, 2)
    value_payloads = _field_values(message, 3)
    keys = [
        item
        for payload in key_payloads
        if isinstance(payload, bytes)
        for item in _packed(payload)
    ]
    values = [
        item
        for payload in value_payloads
        if isinstance(payload, bytes)
        for item in _packed(payload)
    ]
    latitude = 1e-9 * (lat_offset + granularity * _zigzag(_scalar(message, 8)))
    longitude = 1e-9 * (lon_offset + granularity * _zigzag(_scalar(message, 9)))
    return node_id, latitude, longitude, _tags(keys, values, strings)


def _dense_nodes(
    message: bytes,
    strings: list[str],
    granularity: int,
    lat_offset: int,
    lon_offset: int,
) -> Iterator[tuple[int, float, float, dict[str, str]]]:
    id_payloads = _field_values(message, 1)
    lat_payloads = _field_values(message, 8)
    lon_payloads = _field_values(message, 9)
    key_value_payloads = _field_values(message, 10)
    ids = [
        item
        for payload in id_payloads
        if isinstance(payload, bytes)
        for item in _packed(payload, zigzag=True, delta=True)
    ]
    latitudes = [
        item
        for payload in lat_payloads
        if isinstance(payload, bytes)
        for item in _packed(payload, zigzag=True, delta=True)
    ]
    longitudes = [
        item
        for payload in lon_payloads
        if isinstance(payload, bytes)
        for item in _packed(payload, zigzag=True, delta=True)
    ]
    key_values = [
        item
        for payload in key_value_payloads
        if isinstance(payload, bytes)
        for item in _packed(payload)
    ]
    if len(ids) != len(latitudes) or len(ids) != len(longitudes):
        raise PBFError("dense node coordinate counts differ")

    tag_position = 0
    for node_id, raw_latitude, raw_longitude in zip(ids, latitudes, longitudes):
        keys: list[int] = []
        values: list[int] = []
        while tag_position < len(key_values) and key_values[tag_position] != 0:
            if tag_position + 1 >= len(key_values):
                raise PBFError("truncated dense node tags")
            keys.append(key_values[tag_position])
            values.append(key_values[tag_position + 1])
            tag_position += 2
        if tag_position >= len(key_values):
            raise PBFError("dense node tag terminator is missing")
        tag_position += 1
        yield (
            node_id,
            1e-9 * (lat_offset + granularity * raw_latitude),
            1e-9 * (lon_offset + granularity * raw_longitude),
            _tags(keys, values, strings),
        )
    if tag_position != len(key_values):
        raise PBFError("unused dense node tags")


def _primitive_block(
    message: bytes,
) -> Iterator[tuple[int, float, float, dict[str, str]]]:
    string_messages = _field_values(message, 1)
    if len(string_messages) != 1 or not isinstance(string_messages[0], bytes):
        raise PBFError("primitive block must have one string table")
    strings = _string_table(string_messages[0])
    granularity = _scalar(message, 17, 100)
    lat_offset = _int64(_scalar(message, 19, 0))
    lon_offset = _int64(_scalar(message, 20, 0))
    for group in _field_values(message, 2):
        if not isinstance(group, bytes):
            raise PBFError("primitive group must be a message")
        for field_number, _, value in _fields(group):
            if not isinstance(value, bytes):
                continue
            if field_number == 1:
                yield _node(value, strings, granularity, lat_offset, lon_offset)
            elif field_number == 2:
                yield from _dense_nodes(
                    value, strings, granularity, lat_offset, lon_offset
                )


def iter_osm_nodes(
    path: str | Path,
) -> Iterator[tuple[int, float, float, dict[str, str]]]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise PBFError(f"OSM PBF is missing or is a symlink: {source}")
    with source.open("rb") as handle:
        while True:
            encoded_header_size = handle.read(4)
            if not encoded_header_size:
                return
            if len(encoded_header_size) != 4:
                raise PBFError("truncated OSM blob header size")
            header_size = struct.unpack(">I", encoded_header_size)[0]
            if not 0 < header_size <= 64 * 1024:
                raise PBFError(f"invalid OSM blob header size: {header_size}")
            header = handle.read(header_size)
            if len(header) != header_size:
                raise PBFError("truncated OSM blob header")
            type_values = _field_values(header, 1)
            if len(type_values) != 1 or not isinstance(type_values[0], bytes):
                raise PBFError("OSM blob header type is missing")
            blob_type = type_values[0].decode("ascii")
            blob_size = _scalar(header, 3)
            if not 0 < blob_size <= 64 * 1024 * 1024:
                raise PBFError(f"invalid OSM blob size: {blob_size}")
            blob = handle.read(blob_size)
            if len(blob) != blob_size:
                raise PBFError("truncated OSM blob")

            raw_values = _field_values(blob, 1)
            zlib_values = _field_values(blob, 3)
            if len(raw_values) == 1 and isinstance(raw_values[0], bytes):
                payload = raw_values[0]
            elif len(zlib_values) == 1 and isinstance(zlib_values[0], bytes):
                try:
                    payload = zlib.decompress(zlib_values[0])
                except zlib.error as exc:
                    raise PBFError(f"invalid zlib OSM blob: {exc}") from exc
            else:
                raise PBFError("OSM blob compression is unsupported")
            raw_size = _scalar(blob, 2, len(payload))
            if len(payload) != raw_size:
                raise PBFError("OSM blob raw size mismatch")
            if blob_type == "OSMData":
                yield from _primitive_block(payload)
            elif blob_type != "OSMHeader":
                raise PBFError(f"unknown OSM blob type: {blob_type}")


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_size += len(chunk)
    return byte_size, digest.hexdigest()


def extract_postcode_nodes(
    pbf_path: str | Path,
    output_path: str | Path,
    *,
    source_lock_path: str | Path,
    manifest_path: str | Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    pbf = Path(pbf_path)
    output = Path(output_path)
    manifest = (
        Path(manifest_path)
        if manifest_path is not None
        else output.with_suffix(".manifest.json")
    )
    if output.resolve() == manifest.resolve():
        raise ValueError("OSM evidence and manifest paths must be distinct")
    for target in (output, manifest):
        if target.exists() and not force:
            raise FileExistsError(
                f"output exists; pass --force to replace it: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)

    lock = load_source_lock(source_lock_path)
    matches = [source for source in lock.sources if source.filename == pbf.name]
    if len(matches) != 1:
        raise ValueError(f"source lock has no unique entry for OSM PBF: {pbf.name}")
    locked_source = matches[0]
    verify_file(pbf, locked_source)

    descriptor, database_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".sqlite", dir=output.parent
    )
    os.close(descriptor)
    database = Path(database_name)
    descriptor, csv_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary_output = Path(csv_name)
    descriptor, manifest_name = tempfile.mkstemp(
        prefix=f".{manifest.name}.", suffix=".tmp", dir=manifest.parent
    )
    os.close(descriptor)
    temporary_manifest = Path(manifest_name)
    statistics = {
        "nodes_scanned": 0,
        "postcode_tagged": 0,
        "accepted": 0,
        "invalid_postcode": 0,
        "invalid_coordinate": 0,
    }
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE evidence (cep TEXT, source TEXT PRIMARY KEY, latitude REAL, longitude REAL) WITHOUT ROWID"
        )
        for node_id, latitude, longitude, tags in iter_osm_nodes(pbf):
            statistics["nodes_scanned"] += 1
            raw_postcode = tags.get("addr:postcode") or tags.get("postal_code")
            if raw_postcode is None:
                continue
            statistics["postcode_tagged"] += 1
            cep = normalize_cep(raw_postcode)
            if cep is None:
                statistics["invalid_postcode"] += 1
                continue
            try:
                point = Point(latitude, longitude, f"openstreetmap:node/{node_id}")
            except ValueError:
                statistics["invalid_coordinate"] += 1
                continue
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?)",
                (cep, point.source, point.latitude, point.longitude),
            )
            statistics["accepted"] += 1
        connection.commit()

        with temporary_output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(("cep", "ibge", "latitude", "longitude", "source"))
            for row in connection.execute(
                "SELECT cep, latitude, longitude, source FROM evidence ORDER BY cep, source"
            ):
                writer.writerow((row[0], "", repr(row[1]), repr(row[2]), row[3]))
            handle.flush()
            os.fsync(handle.fileno())
        evidence_bytes, evidence_sha256 = _sha256(temporary_output)
        lock_bytes, lock_sha256 = _sha256(Path(source_lock_path))
        manifest_document = {
            "format": "opencepgeo-osm-evidence-manifest-v1",
            "offline": True,
            "source_lock": {
                "filename": Path(source_lock_path).name,
                "bytes": lock_bytes,
                "sha256": lock_sha256,
            },
            "source": locked_source.metadata,
            "filter": "explicit addr:postcode or postal_code tagged nodes only; street-only evidence rejected",
            "statistics": statistics,
            "artifact": {
                "filename": output.name,
                "bytes": evidence_bytes,
                "sha256": evidence_sha256,
            },
            "attribution": "OpenStreetMap contributors; ODbL 1.0; extract by Geofabrik GmbH",
            "publication_gate": "requires-odbl-compliance-and-opencep-dne-clearance",
        }
        with temporary_manifest.open("w", encoding="utf-8", newline="") as handle:
            json.dump(
                manifest_document,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_output, output)
        os.replace(temporary_manifest, manifest)
        return {**statistics, "bytes": evidence_bytes, "sha256": evidence_sha256}
    finally:
        connection.close()
        database.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
