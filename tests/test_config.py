import json
import tempfile
import unittest
from pathlib import Path

from opencepgeo.config import load_enrichment_config


class EnrichmentConfigTests(unittest.TestCase):
    def test_repository_config_is_versioned_and_checksum_bound(self):
        path = Path("config/enrichment-v1.json")
        config, metadata = load_enrichment_config(path)
        self.assertEqual(config.version, "enrichment-2026.2.1-v2")
        self.assertEqual(config.max_osm_municipality_distance_km, 250.0)
        self.assertEqual(metadata["filename"], path.name)
        self.assertEqual(len(metadata["sha256"]), 64)

    def test_missing_threshold_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "opencepgeo-enrichment-v1",
                        "version": "broken",
                        "thresholds": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "min_prefix_samples"):
                load_enrichment_config(path)


if __name__ == "__main__":
    unittest.main()
