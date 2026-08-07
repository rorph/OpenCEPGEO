import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from opencepgeo.database import build_database
from opencepgeo.quality import build_quality_report


def _write_ibge(path: Path, *, sp=(-23.5505, -46.6333)) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT);
        INSERT INTO gpkg_contents VALUES ('localities', 'features');
        CREATE TABLE localities (
            CD_MUN TEXT,
            CT_LOCALIDADE TEXT,
            LAT_LOCALIDADE REAL,
            LONG_LOCALIDADE REAL
        );
        INSERT INTO localities VALUES
            ('3550308', 'Cidade', {sp[0]}, {sp[1]}),
            ('3304557', 'Cidade', -22.9111, -43.2057);
        """
    )
    connection.commit()
    connection.close()


def _write_opencep(path: Path) -> None:
    records = {
        "v1/01001000.json": {
            "cep": "01001000",
            "logradouro": "Praça da Sé",
            "localidade": "São Paulo",
            "uf": "SP",
            "ibge": "3550308",
        },
        "v1/20010000.json": {
            "cep": "20010000",
            "localidade": "Rio de Janeiro",
            "uf": "RJ",
            "ibge": "3304557",
        },
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, record in records.items():
            archive.writestr(name, json.dumps(record))


def _bucket(cep: str, latitude: float, longitude: float, source: str) -> int:
    key = f"{cep}|{latitude:.7f}|{longitude:.7f}|{source}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 2


def _point_for_bucket(cep: str, wanted: int, base: tuple[float, float]):
    for index in range(100):
        latitude = base[0] + index / 10000
        longitude = base[1]
        source = f"openstreetmap:node/{index}"
        if _bucket(cep, latitude, longitude, source) == wanted:
            return latitude, longitude, source
    raise AssertionError("failed to create deterministic bucket fixture")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["cep", "ibge", "latitude", "longitude", "source"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _write_policy(path: Path, *, minimum_records: int = 2) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "opencepgeo-quality-policy-v1",
                "version": "fixture-quality-v1",
                "brazil_bounds": {
                    "latitude_min": -34.0,
                    "latitude_max": 5.5,
                    "longitude_min": -74.0,
                    "longitude_max": -28.0,
                },
                "build_thresholds": {
                    "minimum_records": minimum_records,
                    "minimum_coverage": 1.0,
                    "maximum_unresolved": 0,
                    "minimum_ufs": 2,
                    "maximum_invalid_bounds": 0,
                    "maximum_uf_ibge_mismatches": 0,
                    "maximum_municipality_conflicts": 0,
                    "allowed_precision_tiers": [
                        "observed_cep",
                        "osm_postcode",
                        "observed_cep_prefix",
                        "municipality",
                    ],
                },
                "holdout": {
                    "algorithm": "sha256-modulus-v1",
                    "modulus": 2,
                    "remainder": 1,
                    "minimum_records": 2,
                    "minimum_official_records": 1,
                    "minimum_ufs": 2,
                    "required_address_classes": [
                        "urban_address_proxy",
                        "rural_or_general_address_proxy",
                    ],
                },
                "error_thresholds_km": {
                    "overall_p95": 2000.0,
                    "official_overall_p95": 2000.0,
                    "osm_postcode_p95": 2000.0,
                    "municipality_p95": 2000.0,
                },
            }
        ),
        encoding="utf-8",
    )


class QualityTests(unittest.TestCase):
    def test_deterministic_report_uses_leakage_controlled_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            source = root / "opencep.zip"
            osm = root / "osm.csv"
            official = root / "official.csv"
            policy = root / "quality.json"
            enrichment = Path("config/enrichment-v1.json")
            _write_ibge(gpkg)
            _write_opencep(source)
            sp_train = _point_for_bucket("01001000", 0, (-23.55, -46.63))
            sp_holdout = _point_for_bucket("01001000", 1, (-23.55, -46.63))
            rj_holdout = _point_for_bucket("20010000", 1, (-22.91, -43.20))
            _write_csv(
                osm,
                [
                    {
                        "cep": "01001000",
                        "ibge": "",
                        "latitude": sp_train[0],
                        "longitude": sp_train[1],
                        "source": sp_train[2],
                    },
                    {
                        "cep": "01001000",
                        "ibge": "",
                        "latitude": sp_holdout[0],
                        "longitude": sp_holdout[1],
                        "source": sp_holdout[2],
                    },
                    {
                        "cep": "20010000",
                        "ibge": "",
                        "latitude": rj_holdout[0],
                        "longitude": rj_holdout[1],
                        "source": rj_holdout[2],
                    },
                ],
            )
            _write_csv(
                official,
                [
                    {
                        "cep": "01001000",
                        "ibge": "3550308",
                        "latitude": -23.55,
                        "longitude": -46.63,
                        "source": "official-fixture",
                    }
                ],
            )
            _write_policy(policy)
            database = root / "artifact.sqlite"
            build_database(
                opencep_path=source,
                ibge_path=gpkg,
                osm_observations_path=osm,
                output_path=database,
                source_version="fixture-v1",
            )
            arguments = {
                "database_path": database,
                "ibge_path": gpkg,
                "osm_observations_path": osm,
                "official_holdout_path": official,
                "enrichment_config_path": enrichment,
                "quality_policy_path": policy,
            }
            first = build_quality_report(**arguments)
            second = build_quality_report(**arguments)
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "pass")
            self.assertEqual(first["metrics"]["precision:osm_postcode"]["count"], 1)
            self.assertEqual(first["metrics"]["precision:municipality"]["count"], 1)
            self.assertEqual(first["official_holdout"]["evaluated_observations"], 1)

    def test_build_gate_rejects_out_of_brazil_coordinate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            source = root / "opencep.zip"
            policy = root / "quality.json"
            _write_ibge(gpkg, sp=(10.0, 10.0))
            _write_opencep(source)
            _write_policy(policy)
            with self.assertRaisesRegex(ValueError, "invalid_brazil_bounds"):
                build_database(
                    opencep_path=source,
                    ibge_path=gpkg,
                    output_path=root / "artifact.sqlite",
                    source_version="fixture-v1",
                    quality_config_path=policy,
                )

    def test_build_gate_rejects_material_count_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            source = root / "opencep.zip"
            policy = root / "quality.json"
            _write_ibge(gpkg)
            _write_opencep(source)
            _write_policy(policy, minimum_records=3)
            with self.assertRaisesRegex(ValueError, "minimum_records"):
                build_database(
                    opencep_path=source,
                    ibge_path=gpkg,
                    output_path=root / "artifact.sqlite",
                    source_version="fixture-v1",
                    quality_config_path=policy,
                )


if __name__ == "__main__":
    unittest.main()
