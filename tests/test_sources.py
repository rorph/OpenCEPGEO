import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from opencepgeo.sources import iter_opencep_records


class OpenCEPCorrectionTests(unittest.TestCase):
    def _archive(self, root: Path) -> tuple[Path, bytes]:
        record = {
            "cep": "70864-040",
            "ibge": "1400100",
            "localidade": "Boa Vista",
            "uf": "RR",
        }
        payload = json.dumps(record, separators=(",", ":")).encode()
        archive = root / "opencep.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("v1/69310188.json", payload)
        return archive, payload

    def _corrections(self, root: Path, payload: bytes) -> Path:
        path = root / "corrections.json"
        path.write_text(
            json.dumps(
                {
                    "format": "opencepgeo-corrections-v1",
                    "source_id": "fixture",
                    "corrections": [
                        {
                            "member": "v1/69310188.json",
                            "source_sha256": hashlib.sha256(payload).hexdigest(),
                            "field": "cep",
                            "from": "70864-040",
                            "to": "69310-188",
                            "reason": "fixture source anomaly",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_member_payload_mismatch_fails_without_audited_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            archive, _ = self._archive(Path(directory))
            with self.assertRaisesRegex(ValueError, "member/CEP mismatch"):
                list(iter_opencep_records(archive))

    def test_checksum_bound_correction_is_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, payload = self._archive(root)
            corrections = self._corrections(root, payload)
            records = list(iter_opencep_records(archive, corrections))
            self.assertEqual(records[0]["cep"], "69310-188")

    def test_changed_correction_target_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, payload = self._archive(root)
            corrections = self._corrections(root, payload)
            document = json.loads(corrections.read_text())
            document["corrections"][0]["source_sha256"] = "0" * 64
            corrections.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum changed"):
                list(iter_opencep_records(archive, corrections))


if __name__ == "__main__":
    unittest.main()
