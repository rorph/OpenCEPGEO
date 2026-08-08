from __future__ import annotations

import hashlib
import math
import struct
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .model import Observation

_SHP_HEADER_BYTES = 100
_SHAPE_TYPE_POLYGON = 5
_MAX_PARTS = 10_000
_MAX_POINTS = 1_000_000
_MAX_RECORD_BYTES = 64 * 1024 * 1024
_MAX_DBF_RECORDS = 1_000_000
_NANODEGREES = 1_000_000_000
_Y_BIN_NANODEGREES = 50_000_000
_MEMBER_LIMITS = {
    ".shp": 512 * 1024 * 1024,
    ".dbf": 64 * 1024 * 1024,
    ".shx": 16 * 1024 * 1024,
    ".prj": 1024 * 1024,
    ".cpg": 1024 * 1024,
}
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 600 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_EXPECTED_CPG = b"UTF-8"
_EXPECTED_PRJ = (
    b'GEOGCS["GCS_SIRGAS_2000",DATUM["D_SIRGAS_2000",'
    b'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    b'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)
_OFFICIAL_BRAZIL_BBOX_LIMITS = (-74.5, -34.5, -28.0, 6.0)
_OPERATIONAL_POLYGONS = {"4300001", "4300002"}
_EXPECTED_DBF_SCHEMA = (
    ("CD_MUN", "C", 7, 0),
    ("NM_MUN", "C", 100, 0),
    ("CD_RGI", "C", 6, 0),
    ("NM_RGI", "C", 100, 0),
    ("CD_RGINT", "C", 4, 0),
    ("NM_RGINT", "C", 100, 0),
    ("CD_UF", "C", 2, 0),
    ("NM_UF", "C", 50, 0),
    ("SIGLA_UF", "C", 2, 0),
    ("CD_REGIA", "C", 1, 0),
    ("NM_REGIA", "C", 20, 0),
    ("SIGLA_RG", "C", 2, 0),
    ("CD_CONCU", "C", 7, 0),
    ("NM_CONCU", "C", 100, 0),
    ("AREA_KM2", "N", 12, 3),
)


class BoundaryPosition(str, Enum):
    OUTSIDE = "outside"
    INTERIOR = "interior"
    BOUNDARY = "boundary"


@dataclass(frozen=True, slots=True)
class BoundarySelection:
    eligible: tuple[Observation, ...]
    interior_target_municipality: tuple[Observation, ...]
    boundary_target_municipality: tuple[Observation, ...]
    outside_target_municipality: tuple[Observation, ...]
    unknown_cep: tuple[Observation, ...]


@dataclass(frozen=True, slots=True)
class _Geometry:
    bbox: tuple[int, int, int, int]
    parts: tuple[int, ...]
    coordinates: tuple[int, ...]
    point_count: int


def _archive_members(
    archive: zipfile.ZipFile,
    expected_members: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError("boundary ZIP contains duplicate member names")
    if any(
        info.is_dir()
        or Path(info.filename).name != info.filename
        or "\\" in info.filename
        or info.flag_bits & 0x1
        for info in infos
    ):
        raise ValueError("boundary ZIP contains unsafe member paths or encryption")
    suffixes = {Path(name).suffix.lower() for name in names}
    if len(infos) != len(_MEMBER_LIMITS) or suffixes != set(_MEMBER_LIMITS):
        raise ValueError("boundary ZIP member set is incomplete or unexpected")
    if len({Path(name).stem.lower() for name in names}) != 1:
        raise ValueError("boundary ZIP member basenames do not match")

    by_suffix: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in infos:
        suffix = Path(info.filename).suffix.lower()
        if info.file_size < 1 or info.file_size > _MEMBER_LIMITS[suffix]:
            raise ValueError(f"boundary ZIP member size is invalid: {info.filename}")
        if (
            info.compress_size < 1
            or info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"boundary ZIP member compression ratio is unsafe: {info.filename}"
            )
        by_suffix[suffix] = info
        total_size += info.file_size
    if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError("boundary ZIP uncompressed size exceeds safety limit")

    if expected_members is not None and set(expected_members) != set(names):
        raise ValueError("boundary ZIP member identities do not match source lock")
    for info in infos:
        digest = hashlib.sha256()
        with archive.open(info) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if expected_members is not None:
            expected = expected_members[info.filename]
            if (
                not isinstance(expected, Mapping)
                or expected.get("bytes") != info.file_size
                or expected.get("sha256") != digest.hexdigest()
            ):
                raise ValueError(
                    f"boundary ZIP member identity does not match source lock: {info.filename}"
                )
    if archive.read(by_suffix[".cpg"]) != _EXPECTED_CPG:
        raise ValueError("boundary CPG must declare UTF-8")
    if archive.read(by_suffix[".prj"]) != _EXPECTED_PRJ:
        raise ValueError("boundary PRJ is not the recognized SIRGAS 2000 CRS")
    return by_suffix


def _dbf_codes(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    strict_official: bool,
) -> list[str | None]:
    with archive.open(info) as handle:
        header = handle.read(32)
        if len(header) != 32:
            raise ValueError("truncated boundary DBF header")
        record_count = struct.unpack_from("<I", header, 4)[0]
        header_size = struct.unpack_from("<H", header, 8)[0]
        record_size = struct.unpack_from("<H", header, 10)[0]
        if (
            record_count < 1
            or record_count > _MAX_DBF_RECORDS
            or header_size < 33
            or header_size > 65535
            or record_size < 2
            or record_size > 65535
        ):
            raise ValueError("invalid boundary DBF dimensions")
        if header_size + record_count * record_size + 1 != info.file_size:
            raise ValueError("boundary DBF declared dimensions do not match member size")
        descriptors = handle.read(header_size - 32)
        if len(descriptors) != header_size - 32 or descriptors[-1] != 0x0D:
            raise ValueError("truncated boundary DBF field descriptors")
        fields: list[tuple[str, str, int, int, int]] = []
        offset = 1
        for position in range(0, len(descriptors) - 1, 32):
            descriptor = descriptors[position : position + 32]
            if len(descriptor) != 32:
                raise ValueError("invalid boundary DBF field descriptor")
            try:
                name = descriptor[:11].split(b"\0", 1)[0].decode("ascii")
                field_type = chr(descriptor[11])
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("invalid boundary DBF field metadata") from exc
            length = descriptor[16]
            decimals = descriptor[17]
            fields.append((name, field_type, length, decimals, offset))
            offset += length
        if offset != record_size:
            raise ValueError("boundary DBF record size does not match fields")
        if strict_official and tuple(field[:4] for field in fields) != _EXPECTED_DBF_SCHEMA:
            raise ValueError("boundary DBF schema does not match locked IBGE schema")
        code_field = next((field for field in fields if field[0] == "CD_MUN"), None)
        if code_field is None:
            raise ValueError("boundary DBF has no CD_MUN field")
        _, _type, code_length, _decimals, code_offset = code_field
        codes: list[str | None] = []
        seen: set[str] = set()
        operational: set[str] = set()
        for _index in range(record_count):
            record = handle.read(record_size)
            if len(record) != record_size:
                raise ValueError("truncated boundary DBF record")
            if record[0:1] == b"*":
                raise ValueError("boundary DBF must not contain deleted records")
            try:
                code = record[code_offset : code_offset + code_length].decode(
                    "ascii"
                ).strip()
            except UnicodeDecodeError as exc:
                raise ValueError("non-ASCII municipality code in boundary DBF") from exc
            if len(code) != 7 or not code.isdigit():
                raise ValueError(f"invalid municipality code in boundary DBF: {code!r}")
            if code in seen:
                raise ValueError(f"duplicate municipality code in boundary DBF: {code}")
            seen.add(code)
            if strict_official and code in _OPERATIONAL_POLYGONS:
                operational.add(code)
                codes.append(None)
            else:
                codes.append(code)
        if handle.read() != b"\x1a":
            raise ValueError("boundary DBF has invalid trailing bytes")
        if strict_official and (
            record_count != 5573
            or operational != _OPERATIONAL_POLYGONS
            or len(seen - _OPERATIONAL_POLYGONS) != 5571
        ):
            raise ValueError("boundary DBF municipality/operational record set is invalid")
        return codes


def _shapefile_header(payload: bytes, expected_size: int, label: str):
    if len(payload) != _SHP_HEADER_BYTES:
        raise ValueError(f"truncated boundary {label} header")
    if struct.unpack_from(">I", payload, 0)[0] != 9994:
        raise ValueError(f"invalid boundary {label} file code")
    if any(payload[position : position + 4] != b"\0\0\0\0" for position in range(4, 24, 4)):
        raise ValueError(f"invalid boundary {label} reserved header fields")
    if struct.unpack_from(">I", payload, 24)[0] * 2 != expected_size:
        raise ValueError(f"boundary {label} declared length mismatch")
    if struct.unpack_from("<I", payload, 28)[0] != 1000:
        raise ValueError(f"unsupported boundary {label} version")
    if struct.unpack_from("<I", payload, 32)[0] != _SHAPE_TYPE_POLYGON:
        raise ValueError(f"boundary {label} must contain Polygon geometry")
    bbox = struct.unpack_from("<4d", payload, 36)
    if not all(math.isfinite(value) for value in bbox):
        raise ValueError(f"boundary {label} has non-finite bounds")
    return bbox


def _shx_entries(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> tuple[tuple[float, float, float, float], list[tuple[int, int]]]:
    with archive.open(info) as handle:
        header = handle.read(_SHP_HEADER_BYTES)
        bbox = _shapefile_header(header, info.file_size, "SHX")
        payload = handle.read()
    if len(payload) % 8:
        raise ValueError("boundary SHX index length is invalid")
    entries = [
        struct.unpack_from(">2I", payload, position)
        for position in range(0, len(payload), 8)
    ]
    if not entries or len(entries) > _MAX_DBF_RECORDS:
        raise ValueError("boundary SHX record count is invalid")
    return bbox, entries


def _validate_official_bbox(bbox: tuple[float, float, float, float]) -> None:
    xmin, ymin, xmax, ymax = bbox
    limit_xmin, limit_ymin, limit_xmax, limit_ymax = _OFFICIAL_BRAZIL_BBOX_LIMITS
    if (
        xmin >= xmax
        or ymin >= ymax
        or xmin < limit_xmin
        or ymin < limit_ymin
        or xmax > limit_xmax
        or ymax > limit_ymax
    ):
        raise ValueError(
            "official boundary bounds are inconsistent with longitude=x, latitude=y"
        )


def _shape_records(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    shx_entries: list[tuple[int, int]],
    expected_bbox: tuple[float, float, float, float],
) -> Iterator[_Geometry]:
    with archive.open(info) as handle:
        header = handle.read(_SHP_HEADER_BYTES)
        header_bbox = _shapefile_header(header, info.file_size, "SHP")
        if header_bbox != expected_bbox:
            raise ValueError("boundary SHP/SHX global bounds do not match")
        offset_bytes = _SHP_HEADER_BYTES
        observed_bbox = [math.inf, math.inf, -math.inf, -math.inf]
        for expected_record, (shx_offset, shx_length) in enumerate(shx_entries, 1):
            record_header = handle.read(8)
            if len(record_header) != 8:
                raise ValueError("truncated boundary SHP record header")
            record_number, content_words = struct.unpack(">2I", record_header)
            if (
                record_number != expected_record
                or shx_offset * 2 != offset_bytes
                or shx_length != content_words
            ):
                raise ValueError("boundary SHP/SHX record alignment mismatch")
            content_size = content_words * 2
            if content_size < 44 or content_size > _MAX_RECORD_BYTES:
                raise ValueError("boundary SHP record length is invalid")
            content = handle.read(content_size)
            if len(content) != content_size:
                raise ValueError("truncated boundary SHP record")
            offset_bytes += 8 + content_size
            if struct.unpack_from("<I", content, 0)[0] != _SHAPE_TYPE_POLYGON:
                raise ValueError("boundary SHP contains null or non-Polygon record")
            bbox = struct.unpack_from("<4d", content, 4)
            part_count, point_count = struct.unpack_from("<2I", content, 36)
            if (
                part_count < 1
                or part_count > _MAX_PARTS
                or point_count < 4
                or point_count > _MAX_POINTS
            ):
                raise ValueError("invalid boundary SHP polygon dimensions")
            points_offset = 44 + part_count * 4
            required_size = points_offset + point_count * 16
            if required_size != content_size:
                raise ValueError("boundary SHP polygon record length mismatch")
            parts = tuple(struct.unpack_from(f"<{part_count}I", content, 44))
            if parts[0] != 0 or parts != tuple(sorted(set(parts))) or parts[-1] >= point_count:
                raise ValueError("invalid boundary SHP polygon part indexes")
            raw_coordinates = struct.unpack_from(
                f"<{point_count * 2}d", content, points_offset
            )
            if not all(math.isfinite(value) for value in raw_coordinates):
                raise ValueError("boundary SHP polygon has non-finite coordinates")
            xs = raw_coordinates[0::2]
            ys = raw_coordinates[1::2]
            calculated_bbox = (min(xs), min(ys), max(xs), max(ys))
            if bbox != calculated_bbox:
                raise ValueError("boundary SHP record bounds do not match coordinates")
            if not (-180 <= bbox[0] <= bbox[2] <= 180 and -90 <= bbox[1] <= bbox[3] <= 90):
                raise ValueError("boundary SHP record bounds are outside lon/lat")
            ends = (*parts[1:], point_count)
            for start, end in zip(parts, ends):
                if end - start < 4:
                    raise ValueError("boundary SHP polygon ring has fewer than four points")
                first = raw_coordinates[start * 2 : start * 2 + 2]
                last = raw_coordinates[(end - 1) * 2 : end * 2]
                if first != last:
                    raise ValueError("boundary SHP polygon ring is not closed")
                if any(
                    raw_coordinates[index * 2 : index * 2 + 2]
                    == raw_coordinates[(index + 1) * 2 : (index + 1) * 2 + 2]
                    for index in range(start, end - 1)
                ):
                    raise ValueError("boundary SHP polygon has consecutive duplicate points")
            for index, value in enumerate(calculated_bbox):
                if index < 2:
                    observed_bbox[index] = min(observed_bbox[index], value)
                else:
                    observed_bbox[index] = max(observed_bbox[index], value)
            coordinates = tuple(round(value * _NANODEGREES) for value in raw_coordinates)
            yield _Geometry(
                tuple(round(value * _NANODEGREES) for value in bbox),
                parts,
                coordinates,
                point_count,
            )
        if handle.read(1):
            raise ValueError("boundary SHP has trailing records not present in SHX")
        if offset_bytes != info.file_size or tuple(observed_bbox) != header_bbox:
            raise ValueError("boundary SHP file length or global bounds mismatch")


def _y_bin(latitude: int) -> int:
    return (latitude + 90 * _NANODEGREES) // _Y_BIN_NANODEGREES


def _classify_points(
    shape: _Geometry,
    points: list[Observation],
) -> dict[str, BoundaryPosition]:
    xmin, ymin, xmax, ymax = shape.bbox
    edges_by_bin: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
    ends = (*shape.parts[1:], shape.point_count)
    for start, end in zip(shape.parts, ends):
        for index in range(start, end - 1):
            x1, y1 = shape.coordinates[index * 2 : index * 2 + 2]
            x2, y2 = shape.coordinates[(index + 1) * 2 : (index + 1) * 2 + 2]
            for bin_number in range(_y_bin(min(y1, y2)), _y_bin(max(y1, y2)) + 1):
                edges_by_bin[bin_number].append((x1, y1, x2, y2))

    results: dict[str, BoundaryPosition] = {}
    for observation in points:
        point = observation.point
        identity = point.evidence_id or point.source
        x = round(point.longitude * _NANODEGREES)
        y = round(point.latitude * _NANODEGREES)
        if x < xmin or x > xmax or y < ymin or y > ymax:
            results[identity] = BoundaryPosition.OUTSIDE
            continue
        inside = False
        position = BoundaryPosition.OUTSIDE
        for x1, y1, x2, y2 in edges_by_bin.get(_y_bin(y), ()):
            cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
            if (
                cross == 0
                and min(x1, x2) <= x <= max(x1, x2)
                and min(y1, y2) <= y <= max(y1, y2)
            ):
                position = BoundaryPosition.BOUNDARY
                break
            if (y1 > y) != (y2 > y):
                ray_cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
                if (ray_cross > 0) == (y2 > y1):
                    inside = not inside
        else:
            position = BoundaryPosition.INTERIOR if inside else BoundaryPosition.OUTSIDE
        results[identity] = position
    return results


def select_municipality_observations(
    path: str | Path,
    observations: Iterable[Observation],
    target_ibge_by_cep: Mapping[str, str],
    *,
    expected_members: Mapping[str, Mapping[str, object]] | None = None,
) -> BoundarySelection:
    samples = tuple(observations)
    by_ibge: dict[str, list[Observation]] = defaultdict(list)
    unknown: list[Observation] = []
    identities: set[str] = set()
    for observation in samples:
        identity = observation.point.evidence_id or observation.point.source
        if identity in identities:
            raise ValueError(f"duplicate boundary observation identity: {identity}")
        identities.add(identity)
        target = target_ibge_by_cep.get(observation.cep)
        if target is None:
            unknown.append(observation)
        else:
            by_ibge[target].append(observation)

    decisions: dict[str, BoundaryPosition] = {}
    found: set[str] = set()
    boundary_path = Path(path)
    if not zipfile.is_zipfile(boundary_path):
        raise ValueError("municipality boundaries must be a ZIP archive")
    try:
        with zipfile.ZipFile(boundary_path) as archive:
            members = _archive_members(archive, expected_members)
            strict_official = members[".dbf"].filename == "BR_Municipios_2024.dbf"
            codes = _dbf_codes(
                archive, members[".dbf"], strict_official=strict_official
            )
            shx_bbox, shx_entries = _shx_entries(archive, members[".shx"])
            if strict_official:
                _validate_official_bbox(shx_bbox)
            if len(shx_entries) != len(codes):
                raise ValueError("boundary SHX/DBF record counts do not match")
            shapes = _shape_records(
                archive, members[".shp"], shx_entries, shx_bbox
            )
            for code, shape in zip(codes, shapes):
                if code is None or code not in by_ibge:
                    continue
                if code in found:
                    raise ValueError(f"duplicate municipality boundary: {code}")
                found.add(code)
                decisions.update(_classify_points(shape, by_ibge[code]))
            if next(shapes, None) is not None:
                raise ValueError("boundary SHP has more records than DBF")
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid or corrupt municipality boundary ZIP") from exc
    missing = sorted(set(by_ibge) - found)
    if missing:
        raise ValueError(
            "municipality boundaries are missing target IBGE code(s): "
            + ", ".join(missing[:10])
        )

    unknown_identities = {
        observation.point.evidence_id or observation.point.source
        for observation in unknown
    }
    eligible = [
        observation
        for observation in samples
        if (observation.point.evidence_id or observation.point.source)
        in unknown_identities
        or decisions.get(observation.point.evidence_id or observation.point.source)
        in {BoundaryPosition.INTERIOR, BoundaryPosition.BOUNDARY}
    ]
    outside = [
        observation
        for observation in samples
        if (observation.point.evidence_id or observation.point.source)
        not in unknown_identities
        and decisions.get(observation.point.evidence_id or observation.point.source)
        == BoundaryPosition.OUTSIDE
    ]
    interior = [
        observation
        for observation in samples
        if decisions.get(observation.point.evidence_id or observation.point.source)
        == BoundaryPosition.INTERIOR
    ]
    boundary = [
        observation
        for observation in samples
        if decisions.get(observation.point.evidence_id or observation.point.source)
        == BoundaryPosition.BOUNDARY
    ]
    return BoundarySelection(
        tuple(eligible),
        tuple(interior),
        tuple(boundary),
        tuple(outside),
        tuple(unknown),
    )
