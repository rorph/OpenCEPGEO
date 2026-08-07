import struct
import zipfile
from pathlib import Path


def write_municipality_boundaries(
    path: Path,
    polygons: list[
        tuple[
            str,
            list[tuple[float, float]] | list[list[tuple[float, float]]],
        ]
    ],
) -> None:
    records: list[bytes] = []
    normalized: list[tuple[str, list[list[tuple[float, float]]]]] = []
    for code, raw_parts in polygons:
        if raw_parts and isinstance(raw_parts[0][0], (int, float)):
            parts = [raw_parts]
        else:
            parts = raw_parts
        rings = [
            ring if ring[0] == ring[-1] else [*ring, ring[0]]
            for ring in parts
        ]
        normalized.append((code, rings))
    all_points = [
        point for _code, rings in normalized for ring in rings for point in ring
    ]
    for record_number, (_code, rings) in enumerate(normalized, 1):
        points = [point for ring in rings for point in ring]
        part_indexes: list[int] = []
        cursor = 0
        for ring in rings:
            part_indexes.append(cursor)
            cursor += len(ring)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        content = b"".join(
            (
                struct.pack(
                    "<I4d2I",
                    5,
                    min(xs),
                    min(ys),
                    max(xs),
                    max(ys),
                    len(rings),
                    len(points),
                ),
                struct.pack(f"<{len(part_indexes)}I", *part_indexes),
                b"".join(struct.pack("<2d", *point) for point in points),
            )
        )
        records.append(
            struct.pack(">2I", record_number, len(content) // 2) + content
        )
    shp_size = 100 + sum(len(record) for record in records)
    xs = [point[0] for point in all_points]
    ys = [point[1] for point in all_points]
    header = bytearray(100)
    struct.pack_into(">I", header, 0, 9994)
    struct.pack_into(">I", header, 24, shp_size // 2)
    struct.pack_into("<2I4d", header, 28, 1000, 5, min(xs), min(ys), max(xs), max(ys))
    shp = bytes(header) + b"".join(records)
    shx_size = 100 + len(records) * 8
    shx_header = bytearray(header)
    struct.pack_into(">I", shx_header, 24, shx_size // 2)
    offset_words = 50
    shx_records = []
    for record in records:
        content_words = (len(record) - 8) // 2
        shx_records.append(struct.pack(">2I", offset_words, content_words))
        offset_words += len(record) // 2
    shx = bytes(shx_header) + b"".join(shx_records)

    record_count = len(polygons)
    dbf_header = bytearray(32)
    dbf_header[0] = 3
    struct.pack_into("<IHH", dbf_header, 4, record_count, 65, 8)
    descriptor = bytearray(32)
    descriptor[:6] = b"CD_MUN"
    descriptor[11] = ord("C")
    descriptor[16] = 7
    dbf = bytes(dbf_header) + bytes(descriptor) + b"\r"
    dbf += b"".join(b" " + code.encode("ascii") for code, _rings in normalized)
    dbf += b"\x1a"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("municipios.shp", shp)
        archive.writestr("municipios.shx", shx)
        archive.writestr("municipios.dbf", dbf)
        archive.writestr("municipios.cpg", "UTF-8")
        archive.writestr(
            "municipios.prj",
            'GEOGCS["GCS_SIRGAS_2000",DATUM["D_SIRGAS_2000",'
            'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
            'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]',
        )
