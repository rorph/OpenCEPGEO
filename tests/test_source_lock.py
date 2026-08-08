import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from opencepgeo.source_lock import (
    SourceLockError,
    fetch_sources,
    load_source_lock,
    verify_sources,
)


class SourceLockTests(unittest.TestCase):
    def _fixture(self, root: Path, *, required: bool = True) -> tuple[Path, bytes]:
        payload = b"locked source bytes\n"
        fixture = root / "fixture.bin"
        fixture.write_bytes(payload)
        lock_directory = root / "sources"
        lock_directory.mkdir()
        lock = {
            "format": "opencepgeo-source-lock-v1",
            "release": "fixture-v1",
            "publication_gate": "blocked-test-only",
            "sources": [
                {
                    "id": "fixture",
                    "role": "test input",
                    "required": required,
                    "version": "1",
                    "filename": "fixture-copy.bin",
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "acquisition": "repository",
                    "local_path": "fixture.bin",
                    "retrieved_at": "2026-08-06T00:00:00Z",
                    "attribution": "Test fixture",
                    "license_status": "test-only",
                    "terms_status": "test-only",
                }
            ],
        }
        lock_path = lock_directory / "lock.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
        return lock_path, payload

    def test_fetches_and_verifies_locked_repository_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, payload = self._fixture(root)
            destination = root / "inputs"

            fetched = fetch_sources(lock_path, destination)
            self.assertEqual((destination / "fixture-copy.bin").read_bytes(), payload)
            self.assertEqual(fetched[0]["id"], "fixture")
            self.assertEqual(verify_sources(lock_path, destination)[0]["bytes"], len(payload))

    def test_missing_required_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root)
            with self.assertRaisesRegex(SourceLockError, "missing"):
                verify_sources(lock_path, root / "inputs")

    def test_changed_existing_input_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root)
            destination = root / "inputs"
            destination.mkdir()
            changed = destination / "fixture-copy.bin"
            changed.write_bytes(b"changed")

            with self.assertRaisesRegex(SourceLockError, "size mismatch"):
                fetch_sources(lock_path, destination)
            self.assertEqual(changed.read_bytes(), b"changed")

    def test_optional_sources_are_selected_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root, required=False)
            destination = root / "inputs"

            self.assertEqual(fetch_sources(lock_path, destination), [])
            self.assertFalse(destination.joinpath("fixture-copy.bin").exists())
            self.assertEqual(
                fetch_sources(lock_path, destination, source_ids=["fixture"])[0]["id"],
                "fixture",
            )

    def test_rejects_non_hexadecimal_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root)
            document = json.loads(lock_path.read_text(encoding="utf-8"))
            document["sources"][0]["sha256"] = "x" * 64
            lock_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SourceLockError, "lowercase hexadecimal"):
                load_source_lock(lock_path)

    def test_rejects_missing_rights_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root)
            for field in (
                "retrieved_at",
                "attribution",
                "license_status",
                "terms_status",
            ):
                with self.subTest(field=field):
                    document = json.loads(lock_path.read_text(encoding="utf-8"))
                    document["sources"][0].pop(field, None)
                    lock_path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(SourceLockError, field):
                        load_source_lock(lock_path)
                    document["sources"][0][field] = "test-only"
                    lock_path.write_text(json.dumps(document), encoding="utf-8")

    def test_rejects_missing_publication_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root)
            document = json.loads(lock_path.read_text(encoding="utf-8"))
            del document["publication_gate"]
            lock_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SourceLockError, "publication_gate"):
                load_source_lock(lock_path)

    def test_rejects_malformed_archive_member_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, _ = self._fixture(root)
            document = json.loads(lock_path.read_text(encoding="utf-8"))
            document["sources"][0]["members"] = {
                "../unsafe.bin": {"bytes": 1, "sha256": "0" * 64}
            }
            lock_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SourceLockError, "unsafe"):
                load_source_lock(lock_path)

            document["sources"][0]["members"] = {
                "safe.bin": {"bytes": 1, "sha256": "0" * 64, "extra": True}
            }
            lock_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SourceLockError, "exactly"):
                load_source_lock(lock_path)

    def test_download_rejects_advertised_size_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, payload = self._fixture(root)
            document = json.loads(lock_path.read_text(encoding="utf-8"))
            source = document["sources"][0]
            source["acquisition"] = "https"
            source["url"] = "https://example.invalid/fixture.bin"
            source.pop("local_path")
            lock_path.write_text(json.dumps(document), encoding="utf-8")
            response = io.BytesIO(payload)
            response.headers = {"Content-Length": str(len(payload) + 1)}
            with mock.patch(
                "opencepgeo.source_lock.urllib.request.urlopen",
                return_value=response,
            ):
                with self.assertRaisesRegex(SourceLockError, "server advertised"):
                    fetch_sources(lock_path, root / "inputs")

    def test_download_rejects_payload_larger_than_locked_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path, payload = self._fixture(root)
            document = json.loads(lock_path.read_text(encoding="utf-8"))
            source = document["sources"][0]
            source["acquisition"] = "https"
            source["url"] = "https://example.invalid/fixture.bin"
            source.pop("local_path")
            lock_path.write_text(json.dumps(document), encoding="utf-8")
            response = io.BytesIO(payload + b"x")
            response.headers = {}
            with mock.patch(
                "opencepgeo.source_lock.urllib.request.urlopen",
                return_value=response,
            ):
                with self.assertRaisesRegex(SourceLockError, "exceeds locked size"):
                    fetch_sources(lock_path, root / "inputs")


if __name__ == "__main__":
    unittest.main()
