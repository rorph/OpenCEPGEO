import csv
import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from opencepgeo.osm import PBFError, extract_postcode_nodes, iter_osm_nodes


def varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def scalar(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def message(field: int, payload: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def packed(field: int, values: list[int]) -> bytes:
    return message(field, b"".join(varint(value) for value in values))


def fixture_pbf(path: Path) -> None:
    strings = (b"", b"addr:postcode", b"01001-000", b"addr:street", b"Rua X")
    string_table = b"".join(message(1, value) for value in strings)
    dense = b"".join(
        (
            packed(1, [zigzag(1), zigzag(1)]),
            packed(8, [zigzag(-235500000), zigzag(1000)]),
            packed(9, [zigzag(-466300000), zigzag(1000)]),
            packed(10, [1, 2, 0, 3, 4, 0]),
        )
    )
    primitive_group = message(2, dense)
    primitive_block = b"".join(
        (message(1, string_table), message(2, primitive_group), scalar(17, 100))
    )
    compressed = zlib.compress(primitive_block)
    blob = scalar(2, len(primitive_block)) + message(3, compressed)
    header = message(1, b"OSMData") + scalar(3, len(blob))
    path.write_bytes(struct.pack(">I", len(header)) + header + blob)


def write_compressed_blob(path: Path, compressed: bytes, raw_size: int) -> None:
    blob = scalar(2, raw_size) + message(3, compressed)
    header = message(1, b"OSMData") + scalar(3, len(blob))
    path.write_bytes(struct.pack(">I", len(header)) + header + blob)


class OSMExtractionTests(unittest.TestCase):
    def test_rejects_declared_decompression_bomb_before_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bomb.osm.pbf"
            write_compressed_blob(path, zlib.compress(b"x"), 32 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(PBFError, "safe limit"):
                list(iter_osm_nodes(path))

    def test_rejects_payload_exceeding_declared_raw_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.osm.pbf"
            write_compressed_blob(path, zlib.compress(b"x" * 1024), 10)
            with self.assertRaisesRegex(PBFError, "exceeds declared"):
                list(iter_osm_nodes(path))

    def test_rejects_trailing_compressed_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trailing.osm.pbf"
            payload = b"not-a-primitive-block"
            write_compressed_blob(
                path, zlib.compress(payload) + zlib.compress(b"trailing"), len(payload)
            )
            with self.assertRaisesRegex(PBFError, "trailing compressed"):
                list(iter_osm_nodes(path))

    def test_extracts_only_explicit_postcode_nodes_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pbf = root / "fixture.osm.pbf"
            fixture_pbf(pbf)
            payload = pbf.read_bytes()
            lock = root / "lock.json"
            lock.write_text(
                json.dumps(
                    {
                        "format": "opencepgeo-source-lock-v1",
                        "release": "fixture-v1",
                        "publication_gate": "blocked-test-only",
                        "sources": [
                            {
                                "id": "fixture-osm",
                                "role": "OSM fixture",
                                "required": False,
                                "version": "1",
                                "filename": pbf.name,
                                "bytes": len(payload),
                                "sha256": hashlib.sha256(payload).hexdigest(),
                                "acquisition": "https",
                                "url": "https://example.invalid/fixture.osm.pbf",
                                "retrieved_at": "2026-08-06T00:00:00Z",
                                "attribution": "OSM fixture",
                                "license_status": "test-only",
                                "terms_status": "test-only",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "osm.csv"
            stats = extract_postcode_nodes(pbf, output, source_lock_path=lock)

            self.assertEqual(stats["nodes_scanned"], 2)
            self.assertEqual(stats["postcode_tagged"], 1)
            self.assertEqual(stats["accepted"], 1)
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["cep"], "01001000")
            self.assertEqual(rows[0]["source"], "openstreetmap:node/1")
            self.assertAlmostEqual(float(rows[0]["latitude"]), -23.55)
            manifest = json.loads(output.with_suffix(".manifest.json").read_text())
            self.assertTrue(manifest["offline"])
            self.assertIn("street-only evidence rejected", manifest["filter"])


if __name__ == "__main__":
    unittest.main()
