from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from opencepgeo.cnefe import overlay_cnefe_candidate

_INHERITED_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE cep_geo (
    cep TEXT PRIMARY KEY,
    prefix TEXT NOT NULL,
    street TEXT,
    complement TEXT,
    unit TEXT,
    neighborhood TEXT,
    city TEXT NOT NULL,
    uf TEXT NOT NULL,
    state TEXT,
    region TEXT,
    ibge TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    precision TEXT,
    method TEXT,
    evidence_count INTEGER,
    evidence_radius_km REAL,
    geo_source TEXT,
    evidence_digest TEXT,
    dataset_version TEXT NOT NULL
) WITHOUT ROWID;
"""
_CNEFE_SCHEMA = """
CREATE TABLE candidate (
    cep TEXT PRIMARY KEY,
    latitude REAL,
    longitude REAL,
    precision TEXT,
    method TEXT,
    evidence_count INTEGER,
    evidence_radius_km REAL,
    sources TEXT,
    dataset_version TEXT,
    evidence_digest TEXT,
    uf_code TEXT,
    ibge TEXT,
    status TEXT
) WITHOUT ROWID;
"""
_DIGEST = "sha256:" + ("a" * 64)
_CNEFE_DIGEST = "sha256:" + ("b" * 64)


def _inherited(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_INHERITED_SCHEMA)
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("format", "opencepgeo-sqlite-v4"),
    )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?)",
        ("dataset_version", "fixture-inherited"),
    )
    connection.executemany(
        """
        INSERT INTO cep_geo (
            cep, prefix, street, complement, unit, neighborhood, city, uf, state,
            region, ibge, latitude, longitude, precision, method, evidence_count,
            evidence_radius_km, geo_source, evidence_digest, dataset_version
        ) VALUES (?, ?, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?)
        """,
        [
            (
                "57920000",
                "57920",
                "Maragogi",
                "AL",
                "2708501",
                -9.3159,
                -35.5617,
                "municipality",
                "ibge_city_reference_with_locality_dispersion",
                50,
                27.461,
                '["ibge-localidades"]',
                _DIGEST,
                "fixture-inherited",
            ),
            (
                "01001000",
                "01001",
                "São Paulo",
                "SP",
                "3550308",
                -23.5505,
                -46.6333,
                "osm_postcode",
                "osm_postcode_median",
                2,
                0.2,
                '["openstreetmap"]',
                _DIGEST,
                "fixture-inherited",
            ),
            (
                "01310100",
                "01310",
                "São Paulo",
                "SP",
                "3550308",
                -23.56,
                -46.65,
                "observed_cep",
                "first_party",
                3,
                0.05,
                '["farmabarato"]',
                _DIGEST,
                "fixture-inherited",
            ),
        ],
    )
    connection.commit()
    connection.close()


def _cnefe(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_CNEFE_SCHEMA)
    connection.executemany(
        """
        INSERT INTO candidate (
            cep, latitude, longitude, precision, method, evidence_count,
            evidence_radius_km, sources, dataset_version, evidence_digest,
            uf_code, ibge, status
        ) VALUES (?, ?, ?, 'observed_cep', 'cnefe_robust_median', ?, ?,
                  '["cnefe2022"]', 'cnefe-2022-censo-demografico', ?, ?, ?, ?)
        """,
        [
            (
                "57920000",
                -9.3150865,
                -35.5645745,
                9094,
                2.441,
                _CNEFE_DIGEST,
                "27",
                "2708501",
                "accepted",
            ),
            (
                "01001000",
                -23.61,
                -46.65,
                10,
                0.4,
                _CNEFE_DIGEST,
                "35",
                "3550308",
                "accepted",
            ),
            (
                "01310100",
                -23.57,
                -46.66,
                8,
                0.3,
                _CNEFE_DIGEST,
                "35",
                "3550308",
                "accepted",
            ),
            (
                "99999999",
                -10.0,
                -40.0,
                5,
                0.2,
                _CNEFE_DIGEST,
                "29",
                "2927408",
                "accepted",
            ),
            (
                "11111111",
                -1.0,
                -1.0,
                4,
                0.1,
                _CNEFE_DIGEST,
                "35",
                "3550308",
                "rejected_radius",
            ),
        ],
    )
    connection.commit()
    connection.close()


class CnefeOverlayTests(unittest.TestCase):
    def test_upgrades_coarser_tiers_and_refuses_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inherited = root / "inherited.sqlite"
            cnefe = root / "cnefe.sqlite"
            output = root / "candidate.sqlite"
            manifest = root / "manifest.json"
            _inherited(inherited)
            _cnefe(cnefe)

            stats = overlay_cnefe_candidate(
                inherited_path=inherited,
                cnefe_path=cnefe,
                output_path=output,
                manifest_path=manifest,
                dataset_version="fixture-cnefe",
            )
            self.assertEqual(stats["upgraded"], 2)
            self.assertEqual(stats["skipped_missing_from_inherited"], 1)
            self.assertEqual(stats["skipped_same_or_better_tier"], 1)
            self.assertEqual(
                stats["status"], "offline-candidate-not-approved-for-promotion"
            )

            connection = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
            rows = {
                row[0]: row
                for row in connection.execute(
                    "SELECT cep, precision, method, latitude, longitude FROM cep_geo"
                )
            }
            self.assertEqual(rows["57920000"][1], "observed_cep")
            self.assertEqual(rows["57920000"][2], "cnefe_robust_median")
            self.assertEqual(rows["57920000"][3], -9.3150865)
            self.assertEqual(rows["01001000"][1], "observed_cep")
            self.assertEqual(rows["01310100"][1], "observed_cep")
            self.assertEqual(rows["01310100"][2], "first_party")
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            self.assertEqual(metadata["dataset_version"], "fixture-cnefe")
            self.assertEqual(metadata["count_tier_observed_cep"], "3")
            connection.close()
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(document["upgraded"], 2)

    def test_rejects_coordinate_moved_off_pinned_cnefe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inherited = root / "inherited.sqlite"
            cnefe = root / "cnefe.sqlite"
            output = root / "candidate.sqlite"
            _inherited(inherited)
            _cnefe(cnefe)
            connection = sqlite3.connect(cnefe)
            connection.execute(
                "UPDATE candidate SET latitude = -1.0 WHERE cep = '57920000'"
            )
            # Leave the overlay function to apply, then corrupt after copy is
            # not reachable; instead feed a CNEFE row whose IBGE matches but
            # the inherited update would disagree if we tamper post-apply.
            connection.commit()
            connection.close()
            overlay_cnefe_candidate(
                inherited_path=inherited,
                cnefe_path=cnefe,
                output_path=output,
                manifest_path=root / "manifest.json",
                dataset_version="fixture-cnefe",
            )
            output.chmod(0o644)
            connection = sqlite3.connect(output)
            connection.execute(
                "UPDATE cep_geo SET latitude = -2.0 WHERE cep = '57920000'"
            )
            connection.commit()
            connection.close()
            from opencepgeo.cnefe import load_accepted_cnefe_points, _verify_overlay

            with self.assertRaises(ValueError) as caught:
                _verify_overlay(
                    output,
                    load_accepted_cnefe_points(cnefe),
                    {"upgraded": 2},
                )
            self.assertIn("does not match pinned CNEFE evidence", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
