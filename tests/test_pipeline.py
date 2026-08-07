import csv
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from opencepgeo.database import build_database, lookup
from opencepgeo.sources import load_ibge_municipality_points


def make_ibge_gpkg(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT);
        INSERT INTO gpkg_contents VALUES ('localities', 'features');
        CREATE TABLE localities (
            CD_MUN TEXT,
            CT_LOCALIDADE TEXT,
            LAT_LOCALIDADE REAL,
            LONG_LOCALIDADE REAL
        );
        INSERT INTO localities VALUES
            ('3550308', 'Cidade', -23.5505, -46.6333),
            ('3550308', 'Povoado', -23.6000, -46.7000),
            ('3304557', 'Cidade', -22.9111, -43.2057);
        """
    )
    connection.commit()
    connection.close()


class PipelineTests(unittest.TestCase):
    def test_builds_and_queries_sqlite_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)

            archive_path = root / "v1.zip"
            records = {
                "v1/01001000.json": {
                    "cep": "01001-000",
                    "logradouro": "Praça da Sé",
                    "localidade": "São Paulo",
                    "uf": "SP",
                    "ibge": "3550308",
                },
                "v1/20010000.json": {
                    "cep": "20010-000",
                    "localidade": "Rio de Janeiro",
                    "uf": "RJ",
                    "ibge": "3304557",
                },
            }
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, record in records.items():
                    archive.writestr(name, json.dumps(record))

            observations = root / "observations.csv"
            with observations.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["cep", "ibge", "latitude", "longitude", "source"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "cep": "01001000",
                        "ibge": "3550308",
                        "latitude": "-23.55",
                        "longitude": "-46.63",
                        "source": "test-store",
                    }
                )

            output = root / "opencepgeo.sqlite"
            stats = build_database(
                opencep_path=archive_path,
                ibge_path=gpkg,
                observations_path=observations,
                output_path=output,
                source_version="fixture-v1",
            )
            self.assertEqual(stats, {"rows": 2, "located": 2, "unresolved": 0})

            exact = lookup(output, "01001-000")
            self.assertEqual(exact["geo"]["precision"], "observed_cep")
            self.assertEqual(exact["geo"]["source"], ["test-store"])

            fallback = lookup(output, "20010000")
            self.assertEqual(fallback["geo"]["precision"], "municipality")
            self.assertEqual(fallback["geo"]["coordinates"], [-43.2057, -22.9111])

    def test_reads_city_points_without_a_gis_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            gpkg = Path(directory) / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            points = load_ibge_municipality_points(gpkg)
            self.assertEqual(set(points), {"3550308", "3304557"})
            self.assertAlmostEqual(points["3550308"].latitude, -23.5505)

    def test_rejects_duplicate_ceps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            source = root / "opencep"
            source.mkdir()
            record = {
                "cep": "01001-000",
                "localidade": "São Paulo",
                "uf": "SP",
                "ibge": "3550308",
            }
            (source / "a.json").write_text(json.dumps(record), encoding="utf-8")
            (source / "b.json").write_text(json.dumps(record), encoding="utf-8")
            output = root / "opencepgeo.sqlite"

            with self.assertRaises(sqlite3.IntegrityError):
                build_database(
                    opencep_path=source,
                    ibge_path=gpkg,
                    output_path=output,
                    source_version="fixture-duplicates",
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
