import hashlib
import json
import os
import resource
import struct
import time
import unittest
import zipfile
from pathlib import Path

from opencepgeo.boundaries import (
    BoundaryPosition,
    _archive_members,
    _classify_points,
    _dbf_codes,
    _shape_records,
    _shx_entries,
)
from opencepgeo.model import Observation, Point


_BOUNDARY_PATH = os.environ.get("OPENCEPGEO_LIVE_BOUNDARIES")
_SOURCE_LOCK_PATH = os.environ.get(
    "OPENCEPGEO_LIVE_SOURCE_LOCK", "sources/lock.json"
)


@unittest.skipUnless(
    _BOUNDARY_PATH,
    "set OPENCEPGEO_LIVE_BOUNDARIES to run the locked 199 MB archive test",
)
class LockedMunicipalityArchiveTests(unittest.TestCase):
    def test_locked_archive_geometry_and_known_points(self):
        started = time.monotonic()
        source_lock = json.loads(
            Path(_SOURCE_LOCK_PATH).read_text(encoding="utf-8")
        )
        source = next(
            item
            for item in source_lock["sources"]
            if item["id"] == "ibge-municipios-2024"
        )
        digest = hashlib.sha256()
        shapes_by_code = {}
        records = municipalities = operational = multipart = 0
        maximum_parts = maximum_points = 0
        with zipfile.ZipFile(_BOUNDARY_PATH) as archive:
            members = _archive_members(archive, source["members"])
            codes = _dbf_codes(archive, members[".dbf"], strict_official=True)
            bbox, entries = _shx_entries(archive, members[".shx"])
            shapes = _shape_records(archive, members[".shp"], entries, bbox)
            for code, shape in zip(codes, shapes, strict=True):
                records += 1
                if code is None:
                    operational += 1
                else:
                    municipalities += 1
                if len(shape.parts) > 1:
                    multipart += 1
                maximum_parts = max(maximum_parts, len(shape.parts))
                maximum_points = max(maximum_points, shape.point_count)
                if code in {"3550308", "3304557", "5300108", "5213103"}:
                    shapes_by_code[code] = shape
                digest.update((code or "OPERATIONAL").encode("ascii"))
                digest.update(struct.pack("<4q", *shape.bbox))
                digest.update(struct.pack(f"<{len(shape.parts)}I", *shape.parts))
                digest.update(
                    struct.pack(f"<{len(shape.coordinates)}q", *shape.coordinates)
                )

        self.assertEqual(records, 5573)
        self.assertEqual(municipalities, 5571)
        self.assertEqual(operational, 2)
        self.assertEqual(multipart, 113)
        self.assertEqual(maximum_parts, 153)
        self.assertEqual(maximum_points, 80174)
        self.assertEqual(
            digest.hexdigest(),
            "d1fcaf664ff3705cbba5e4cf927470443cf5f9185e8b7094fe975c83ead0cbd5",
        )

        cases = (
            ("3550308", -23.5505, -46.6333, BoundaryPosition.INTERIOR),
            ("3304557", -22.9068, -43.1729, BoundaryPosition.INTERIOR),
            ("5300108", -15.7939, -47.8828, BoundaryPosition.INTERIOR),
            # A point inside a real interior ring of Mineiros/GO must classify
            # outside the municipality under orientation-independent parity.
            ("5213103", -17.474158818, -52.884755654, BoundaryPosition.OUTSIDE),
        )
        for index, (code, latitude, longitude, expected) in enumerate(cases):
            identity = f"live-boundary-{index}"
            observation = Observation(
                f"{index:08d}",
                Point(latitude, longitude, identity, identity),
            )
            self.assertEqual(
                _classify_points(shapes_by_code[code], [observation])[identity],
                expected,
            )

        elapsed = time.monotonic() - started
        maximum_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.assertLess(elapsed, 60.0)
        self.assertLess(maximum_rss_kb, 256 * 1024)


if __name__ == "__main__":
    unittest.main()
