import csv
import hashlib
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
                    fieldnames=[
                        "cep",
                        "ibge",
                        "latitude",
                        "longitude",
                        "source",
                        "evidence_id",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "cep": "01001000",
                        "ibge": "3550308",
                        "latitude": "-23.55",
                        "longitude": "-46.63",
                        "source": "test-store",
                        "evidence_id": "test-store:location/1",
                    }
                )

            output = root / "opencepgeo.sqlite"
            export = root / "opencepgeo.jsonl"
            manifest = root / "opencepgeo.manifest.json"
            stats = build_database(
                opencep_path=archive_path,
                ibge_path=gpkg,
                observations_path=observations,
                output_path=output,
                export_path=export,
                manifest_path=manifest,
                source_version="fixture-v1",
            )
            self.assertEqual(stats["input_records"], 2)
            self.assertEqual(stats["unique_ceps"], 2)
            self.assertEqual(stats["ibge_join_rate"], 1.0)
            self.assertEqual(stats["located"], 2)
            self.assertEqual(stats["unresolved"], 0)

            normalized_rows = [
                json.loads(line) for line in export.read_text().splitlines()
            ]
            self.assertEqual(
                [row["cep"] for row in normalized_rows], ["01001000", "20010000"]
            )
            build_manifest = json.loads(manifest.read_text())
            self.assertEqual(build_manifest["format"], "opencepgeo-build-manifest-v2")
            self.assertEqual(build_manifest["schema_version"], "opencepgeo-sqlite-v4")
            self.assertRegex(
                build_manifest["builder"]["source_tree_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertEqual(
                build_manifest["artifacts"]["normalized"]["sha256"],
                hashlib.sha256(export.read_bytes()).hexdigest(),
            )

            exact = lookup(output, "01001-000")
            self.assertEqual(exact["geo"]["precision"], "observed_cep")
            self.assertEqual(exact["geo"]["source"], ["test-store"])
            self.assertRegex(exact["geo"]["evidence_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(exact["geo"]["method"], "robust_median_first_party")
            self.assertEqual(exact["geo"]["evidence_count"], 1)
            self.assertIsInstance(exact["geo"]["evidence_radius_km"], float)
            self.assertEqual(exact["dataset_version"], "fixture-v1")

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

    def test_reads_single_geopackage_from_locked_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            archive = root / "ibge.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.write(gpkg, "BR_localidades_2022.gpkg")
            points = load_ibge_municipality_points(archive)
            self.assertEqual(set(points), {"3550308", "3304557"})

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

    def test_rejects_invalid_cep_instead_of_skipping_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            source = root / "opencep"
            source.mkdir()
            (source / "bad.json").write_text(
                json.dumps(
                    {
                        "cep": "invalid",
                        "localidade": "Sao Paulo",
                        "uf": "SP",
                        "ibge": "3550308",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid CEP"):
                build_database(
                    opencep_path=source,
                    ibge_path=gpkg,
                    output_path=root / "out.sqlite",
                    source_version="fixture-invalid",
                )

    def test_repeated_builds_have_identical_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            source = root / "opencep"
            source.mkdir()
            records = [
                {
                    "cep": "20010000",
                    "localidade": "Rio de Janeiro",
                    "uf": "RJ",
                    "ibge": "3304557",
                },
                {
                    "cep": "01001000",
                    "localidade": "Sao Paulo",
                    "uf": "SP",
                    "ibge": "3550308",
                },
            ]
            for index, record in enumerate(records):
                (source / f"{index}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )

            artifacts = []
            for suffix in ("one", "two"):
                output = root / f"{suffix}.sqlite"
                export = root / f"{suffix}.jsonl"
                manifest = root / f"{suffix}.manifest.json"
                build_database(
                    opencep_path=source,
                    ibge_path=gpkg,
                    output_path=output,
                    export_path=export,
                    manifest_path=manifest,
                    source_version="fixture-deterministic",
                )
                artifacts.append((output.read_bytes(), export.read_bytes()))

            self.assertEqual(artifacts[0], artifacts[1])
            rows = [json.loads(line) for line in artifacts[0][1].splitlines()]
            self.assertEqual([row["cep"] for row in rows], ["01001000", "20010000"])

    def test_requires_source_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            source = root / "opencep"
            source.mkdir()
            with self.assertRaisesRegex(
                ValueError, "source_version or source_lock_path"
            ):
                build_database(
                    opencep_path=source,
                    ibge_path=gpkg,
                    output_path=root / "out.sqlite",
                )

    def test_build_uses_separate_osm_postcode_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpkg = root / "ibge.gpkg"
            make_ibge_gpkg(gpkg)
            source = root / "opencep"
            source.mkdir()
            (source / "01001000.json").write_text(
                json.dumps(
                    {
                        "cep": "01001-000",
                        "localidade": "Sao Paulo",
                        "uf": "SP",
                        "ibge": "3550308",
                    }
                ),
                encoding="utf-8",
            )
            osm = root / "osm.csv"
            osm.write_text(
                "cep,ibge,latitude,longitude,source\n"
                "01001000,,-23.5501,-46.6334,openstreetmap:node/1\n",
                encoding="utf-8",
            )
            output = root / "out.sqlite"
            stats = build_database(
                opencep_path=source,
                ibge_path=gpkg,
                osm_observations_path=osm,
                output_path=output,
                source_version="fixture-osm",
            )
            result = lookup(output, "01001000")
            self.assertEqual(result["geo"]["precision"], "osm_postcode")
            self.assertEqual(stats["tier_osm_postcode"], 1)


if __name__ == "__main__":
    unittest.main()
