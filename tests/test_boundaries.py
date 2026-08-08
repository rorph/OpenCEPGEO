import hashlib
import random
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from opencepgeo.boundaries import (
    BoundaryPosition,
    _Geometry,
    _classify_points,
    _validate_official_bbox,
    select_municipality_observations,
)
from opencepgeo.model import Observation, Point
from tests.helpers import write_municipality_boundaries


def _observation(
    cep: str, latitude: float, longitude: float, identity: str
) -> Observation:
    evidence_id = f"openstreetmap:node/{identity}"
    return Observation(
        cep,
        Point(latitude, longitude, evidence_id, evidence_id),
    )


def _members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _write_members(
    path: Path,
    members: dict[str, bytes],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def _fixture(path: Path) -> None:
    write_municipality_boundaries(
        path,
        [
            (
                "3550308",
                [
                    (-47.0, -24.0),
                    (-46.0, -24.0),
                    (-46.0, -23.0),
                    (-47.0, -23.0),
                ],
            )
        ],
    )


class MunicipalityBoundaryTests(unittest.TestCase):
    def test_selects_inside_boundary_and_unknown_but_excludes_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary_path = Path(directory) / "boundaries.zip"
            _fixture(boundary_path)
            inside = _observation("01001000", -23.5, -46.5, "1")
            boundary = _observation("01001000", -23.5, -47.0, "2")
            outside = _observation("01001000", -22.0, -46.5, "3")
            unknown = _observation("99999999", -22.0, -46.5, "4")

            selection = select_municipality_observations(
                boundary_path,
                [inside, boundary, outside, unknown],
                {"01001000": "3550308"},
            )

            self.assertEqual(selection.eligible, (inside, boundary, unknown))
            self.assertEqual(selection.outside_target_municipality, (outside,))
            self.assertEqual(selection.unknown_cep, (unknown,))

    def test_handles_holes_multipart_reversed_orientation_and_tiny_slivers(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary_path = Path(directory) / "boundaries.zip"
            write_municipality_boundaries(
                boundary_path,
                [
                    (
                        "3550308",
                        [
                            [(0, 0), (0, 10), (10, 10), (10, 0)],
                            [(3, 3), (7, 3), (7, 7), (3, 7)],
                            [(20, 0), (21, 0), (21, 1), (20, 1)],
                            [
                                (30, 0),
                                (30.000000003, 0),
                                (30.000000003, 0.000000003),
                                (30, 0.000000003),
                            ],
                        ],
                    )
                ],
            )
            mainland = _observation("01001000", 1, 1, "mainland")
            hole = _observation("01001000", 5, 5, "hole")
            island = _observation("01001000", 0.5, 20.5, "island")
            sliver = _observation(
                "01001000", 0.000000001, 30.000000001, "sliver"
            )

            selection = select_municipality_observations(
                boundary_path,
                [mainland, hole, island, sliver],
                {"01001000": "3550308"},
            )

            self.assertEqual(selection.eligible, (mainland, island, sliver))
            self.assertEqual(selection.outside_target_municipality, (hole,))

    def test_shared_border_vertex_and_horizontal_edges_are_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary_path = Path(directory) / "boundaries.zip"
            write_municipality_boundaries(
                boundary_path,
                [
                    ("3550308", [(0, 0), (1, 0), (1, 1), (0, 1)]),
                    ("3304557", [(1, 0), (2, 0), (2, 1), (1, 1)]),
                ],
            )
            left_border = _observation("01001000", 0.5, 1, "left-border")
            right_border = _observation("20010000", 0.5, 1, "right-border")
            horizontal = _observation("01001000", 0, 0.5, "horizontal")
            vertex = _observation("01001000", 1, 1, "vertex")

            selection = select_municipality_observations(
                boundary_path,
                [left_border, right_border, horizontal, vertex],
                {"01001000": "3550308", "20010000": "3304557"},
            )

            self.assertEqual(
                selection.eligible,
                (left_border, right_border, horizontal, vertex),
            )
            self.assertEqual(selection.outside_target_municipality, ())

    def test_half_open_ray_rule_handles_vertex_crossing(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary_path = Path(directory) / "boundaries.zip"
            write_municipality_boundaries(
                boundary_path,
                [("3550308", [(0, 1), (1, 2), (2, 1), (1, 0)])],
            )
            interior = _observation("01001000", 1, 1, "interior")
            outside = _observation("01001000", 1, 3, "outside")

            selection = select_municipality_observations(
                boundary_path,
                [interior, outside],
                {"01001000": "3550308"},
            )

            self.assertEqual(selection.eligible, (interior,))
            self.assertEqual(selection.outside_target_municipality, (outside,))

    def test_indexed_classifier_matches_brute_force_parity(self):
        scale = 1_000_000_000
        rings = (
            ((0, 0), (10, 0), (10, 10), (0, 10), (0, 0)),
            ((3, 3), (3, 7), (7, 7), (7, 3), (3, 3)),
            ((20, 0), (21, 0), (21, 1), (20, 1), (20, 0)),
        )
        parts: list[int] = []
        coordinates: list[int] = []
        for ring in rings:
            parts.append(len(coordinates) // 2)
            for longitude, latitude in ring:
                coordinates.extend((longitude * scale, latitude * scale))
        shape = _Geometry(
            (0, 0, 21 * scale, 10 * scale),
            tuple(parts),
            tuple(coordinates),
            len(coordinates) // 2,
        )
        generator = random.Random(181)
        observations = [
            _observation(
                "01001000",
                generator.uniform(-1, 11),
                generator.uniform(-1, 22),
                str(index),
            )
            for index in range(1000)
        ]

        indexed = _classify_points(shape, observations)

        def brute(observation: Observation) -> BoundaryPosition:
            x = round(observation.point.longitude * scale)
            y = round(observation.point.latitude * scale)
            inside = False
            ends = (*shape.parts[1:], shape.point_count)
            for start, end in zip(shape.parts, ends):
                for index in range(start, end - 1):
                    x1, y1 = shape.coordinates[index * 2 : index * 2 + 2]
                    x2, y2 = shape.coordinates[(index + 1) * 2 : (index + 1) * 2 + 2]
                    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
                    if (
                        cross == 0
                        and min(x1, x2) <= x <= max(x1, x2)
                        and min(y1, y2) <= y <= max(y1, y2)
                    ):
                        return BoundaryPosition.BOUNDARY
                    if (y1 > y) != (y2 > y):
                        ray_cross = (x2 - x1) * (y - y1) - (x - x1) * (y2 - y1)
                        if (ray_cross > 0) == (y2 > y1):
                            inside = not inside
            return BoundaryPosition.INTERIOR if inside else BoundaryPosition.OUTSIDE

        self.assertEqual(
            indexed,
            {
                observation.point.evidence_id: brute(observation)
                for observation in observations
            },
        )

    def test_missing_target_municipality_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary_path = Path(directory) / "boundaries.zip"
            _fixture(boundary_path)
            observation = _observation("20010000", -22.9, -43.2, "4")
            with self.assertRaisesRegex(ValueError, "3304557"):
                select_municipality_observations(
                    boundary_path,
                    [observation],
                    {"20010000": "3304557"},
                )

    def test_duplicate_observation_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary_path = Path(directory) / "boundaries.zip"
            _fixture(boundary_path)
            observations = [
                _observation("01001000", -23.5, -46.5, "same"),
                _observation("01001000", -23.6, -46.6, "same"),
            ]
            with self.assertRaisesRegex(ValueError, "duplicate boundary observation"):
                select_municipality_observations(
                    boundary_path,
                    observations,
                    {"01001000": "3550308"},
                )

    def test_rejects_unsafe_duplicate_and_unexpected_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.zip"
            _fixture(original)
            members = _members(original)
            cases: list[tuple[str, dict[str, bytes], str]] = []
            traversal = dict(members)
            traversal["../municipios.cpg"] = traversal.pop("municipios.cpg")
            cases.append(("traversal", traversal, "unsafe"))
            unexpected = dict(members)
            unexpected["README.txt"] = b"unexpected"
            cases.append(("unexpected", unexpected, "incomplete or unexpected"))
            for name, payloads, message in cases:
                with self.subTest(name=name):
                    candidate = root / f"{name}.zip"
                    _write_members(candidate, payloads)
                    with self.assertRaisesRegex(ValueError, message):
                        select_municipality_observations(candidate, [], {})

            duplicate = root / "duplicate.zip"
            _write_members(duplicate, members)
            with zipfile.ZipFile(duplicate, "a") as archive:
                archive.writestr("municipios.cpg", b"UTF-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                select_municipality_observations(duplicate, [], {})

    def test_rejects_encryption_flag_compression_bomb_and_crc_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.zip"
            _fixture(original)
            members = _members(original)

            with zipfile.ZipFile(original) as archive:
                infos = archive.infolist()
                infos[0].flag_bits |= 0x1
                with self.assertRaisesRegex(ValueError, "encryption"):
                    from opencepgeo.boundaries import _archive_members

                    _archive_members(archive, None)

            bomb = root / "bomb.zip"
            bomb_members = dict(members)
            bomb_members["municipios.cpg"] = b"A" * 100_000
            _write_members(bomb, bomb_members, compression=zipfile.ZIP_DEFLATED)
            with self.assertRaisesRegex(ValueError, "compression ratio"):
                select_municipality_observations(bomb, [], {})

            corrupt = root / "crc.zip"
            corrupt.write_bytes(original.read_bytes())
            payload = bytearray(corrupt.read_bytes())
            offset = payload.find(b"UTF-8")
            self.assertGreaterEqual(offset, 0)
            payload[offset] ^= 1
            corrupt.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, "corrupt"):
                select_municipality_observations(corrupt, [], {})

    def test_rejects_member_identity_and_crs_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.zip"
            _fixture(original)
            members = _members(original)
            identities = {
                name: {
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in members.items()
            }
            identities["municipios.shp"]["sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "source lock"):
                select_municipality_observations(
                    original, [], {}, expected_members=identities
                )

            wrong_crs = root / "wrong-crs.zip"
            members["municipios.prj"] = b'GEOGCS["WGS 84"]'
            _write_members(wrong_crs, members)
            with self.assertRaisesRegex(ValueError, "SIRGAS"):
                select_municipality_observations(wrong_crs, [], {})

    def test_official_bounds_reject_lon_lat_swaps(self):
        _validate_official_bbox((-74.0, -34.0, -28.5, 5.5))
        with self.assertRaisesRegex(ValueError, "longitude=x"):
            _validate_official_bbox((-34.0, -74.0, 5.5, -28.5))

    def test_rejects_corrupt_shx_dbf_parts_and_unclosed_rings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.zip"
            _fixture(original)
            originals = _members(original)

            corruptions: list[tuple[str, str, int, bytes, str]] = [
                ("shx", "municipios.shx", 100, struct.pack(">I", 51), "alignment"),
                ("dbf", "municipios.dbf", 65, b"*", "deleted"),
                ("dbf-header", "municipios.dbf", 4, struct.pack("<I", 2), "dimensions"),
                ("shp-header", "municipios.shp", 0, struct.pack(">I", 9995), "file code"),
                ("parts", "municipios.shp", 152, struct.pack("<I", 1), "part indexes"),
                (
                    "unclosed",
                    "municipios.shp",
                    220,
                    struct.pack("<d", -46.75),
                    "not closed",
                ),
            ]
            for name, member_name, offset, replacement, message in corruptions:
                with self.subTest(name=name):
                    members = dict(originals)
                    member = bytearray(members[member_name])
                    member[offset : offset + len(replacement)] = replacement
                    members[member_name] = bytes(member)
                    candidate = root / f"{name}.zip"
                    _write_members(candidate, members)
                    with self.assertRaisesRegex(ValueError, message):
                        select_municipality_observations(candidate, [], {})

    def test_rejects_official_filename_with_nonofficial_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.zip"
            _fixture(original)
            renamed = {
                name.replace("municipios", "BR_Municipios_2024"): payload
                for name, payload in _members(original).items()
            }
            candidate = root / "official-name.zip"
            _write_members(candidate, renamed)
            with self.assertRaisesRegex(ValueError, "schema"):
                select_municipality_observations(candidate, [], {})


if __name__ == "__main__":
    unittest.main()
