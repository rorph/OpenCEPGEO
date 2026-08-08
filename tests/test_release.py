import csv
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from opencepgeo.database import build_database, lookup
from opencepgeo.quality import (
    build_quality_report,
    quality_report_markdown,
    write_quality_report,
)
from opencepgeo.release import (
    _CSV_COLUMNS,
    _CSV_FORMAT,
    _JSONL_FORMAT,
    _verify_sorted_export,
    package_release,
    verify_release,
)
from tests.helpers import write_municipality_boundaries


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_inputs(root: Path) -> tuple[Path, Path]:
    gpkg = root / "ibge.gpkg"
    connection = sqlite3.connect(gpkg)
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
            ('3304557', 'Cidade', -22.9111, -43.2057);
        """
    )
    connection.commit()
    connection.close()
    source = root / "opencep.zip"
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
        "v1/53990959.json": {
            "cep": "53990959",
            "localidade": "Fernando de Noronha",
            "uf": "PE",
            "ibge": "2605459",
        },
    }
    with zipfile.ZipFile(source, "w") as archive:
        for name, record in records.items():
            archive.writestr(name, json.dumps(record))
    return source, gpkg


def _write_source_lock(
    path: Path, source: Path, gpkg: Path, boundaries: Path
) -> None:
    with zipfile.ZipFile(boundaries) as archive:
        boundary_members = {
            info.filename: {
                "bytes": info.file_size,
                "sha256": hashlib.sha256(archive.read(info)).hexdigest(),
            }
            for info in archive.infolist()
        }
    path.write_text(
        json.dumps(
            {
                "format": "opencepgeo-source-lock-v1",
                "release": "fixture-release-v1",
                "publication_gate": "blocked-fixture-rights-review",
                "sources": [
                    {
                        "id": "opencep-fixture",
                        "role": "fixture CEP corpus",
                        "required": True,
                        "version": "fixture-v1",
                        "filename": source.name,
                        "bytes": source.stat().st_size,
                        "sha256": _sha256(source),
                        "acquisition": "https",
                        "url": "https://example.invalid/opencep.zip",
                        "retrieved_at": "2026-08-06T00:00:00Z",
                        "attribution": "OpenCEP fixture",
                        "license_status": "test-only",
                        "terms_status": "test-only",
                    },
                    {
                        "id": "boundary-fixture",
                        "role": "fixture municipality polygons",
                        "required": False,
                        "version": "fixture-v1",
                        "filename": boundaries.name,
                        "bytes": boundaries.stat().st_size,
                        "sha256": _sha256(boundaries),
                        "acquisition": "https",
                        "url": "https://example.invalid/boundaries.zip",
                        "retrieved_at": "2026-08-06T00:00:00Z",
                        "attribution": "IBGE boundary fixture",
                        "license_status": "test-only",
                        "terms_status": "test-only",
                        "members": boundary_members,
                    },
                    {
                        "id": "ibge-fixture",
                        "role": "fixture municipality points",
                        "required": True,
                        "version": "fixture-v1",
                        "filename": gpkg.name,
                        "bytes": gpkg.stat().st_size,
                        "sha256": _sha256(gpkg),
                        "acquisition": "https",
                        "url": "https://example.invalid/ibge.gpkg",
                        "retrieved_at": "2026-08-06T00:00:00Z",
                        "attribution": "IBGE fixture",
                        "license_status": "test-only",
                        "terms_status": "test-only",
                    },
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_quality_policy(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "opencepgeo-quality-policy-v2",
                "version": "fixture-quality-v1",
                "brazil_bounds": {
                    "latitude_min": -34.0,
                    "latitude_max": 5.5,
                    "longitude_min": -74.0,
                    "longitude_max": -28.0,
                },
                "build_thresholds": {
                    "minimum_records": 3,
                    "minimum_coverage": 0.66,
                    "maximum_unresolved": 1,
                    "minimum_ufs": 3,
                    "maximum_invalid_bounds": 0,
                    "maximum_uf_ibge_mismatches": 0,
                    "maximum_municipality_conflicts": 0,
                    "maximum_geo_source_bytes": 2048,
                    "maximum_evidence_digest_bytes": 71,
                    "maximum_invalid_evidence_digests": 0,
                    "allowed_precision_tiers": [
                        "observed_cep",
                        "osm_postcode",
                        "observed_cep_prefix",
                        "municipality",
                    ],
                },
                "validation": {
                    "algorithm": "sha256-modulus-v2",
                    "modulus": 2,
                    "remainder": 0,
                    "osm_evidence": {
                        "maximum_outside_target_municipality_fraction": 0.01,
                    },
                    "cohorts": {
                        name: {
                            "minimum_records": 2,
                            "maximum_missing_fraction": 0.0,
                            "maximum_prediction_failure_fraction": 0.0,
                            "minimum_ufs": 2,
                            "required_address_classes": [],
                        }
                        for name in ("leave_observation_out", "unseen_cep")
                    },
                    "per_uf": {
                        "cohort": "unseen_cep",
                        "required_ufs": ["SP", "RJ"],
                        "thresholds": {
                            uf: {
                                "minimum_samples": 1,
                                "maximum_p95_km": 2000.0,
                            }
                            for uf in ("SP", "RJ")
                        },
                    },
                    "purposes": {
                        "nearby": {
                            "cohort": "leave_observation_out",
                            "allowed_precision_tiers": ["osm_postcode"],
                            "minimum_records": 1,
                            "maximum_p95_km": 2000.0,
                        },
                        "fallback": {
                            "cohort": "unseen_cep",
                            "allowed_precision_tiers": ["municipality"],
                            "minimum_records": 1,
                            "maximum_p95_km": 2000.0,
                        },
                    },
                    "official_pilot": {
                        "minimum_records": 1,
                        "maximum_missing_fraction": 0.0,
                        "maximum_prediction_failure_fraction": 0.0,
                        "expected_ufs": ["SP"],
                        "maximum_p95_km": 2000.0,
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _source_for_bucket(cep: str, wanted: int) -> str:
    for index in range(100):
        source = f"openstreetmap:node/{cep}{index}"
        bucket = int.from_bytes(hashlib.sha256(source.encode()).digest()[:8], "big") % 2
        if bucket == wanted:
            return source
    raise AssertionError("could not create source bucket")


def _write_validation_inputs(
    root: Path, source_lock: Path
) -> tuple[Path, Path]:
    osm = root / "osm.csv"
    with osm.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["cep", "ibge", "latitude", "longitude", "source"],
        )
        writer.writeheader()
        for cep, latitude, longitude in (
            ("01001000", -23.5505, -46.6333),
            ("20010000", -22.9111, -43.2057),
        ):
            for bucket in (0, 1):
                writer.writerow(
                    {
                        "cep": cep,
                        "ibge": "",
                        "latitude": latitude,
                        "longitude": longitude,
                        "source": _source_for_bucket(cep, bucket),
                    }
                )
    artifact = {
        "filename": osm.name,
        "bytes": osm.stat().st_size,
        "sha256": _sha256(osm),
    }
    osm.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "format": "opencepgeo-osm-evidence-manifest-v1",
                "source_lock": {
                    "sha256": _sha256(source_lock),
                },
                "source": {"id": "fixture-osm"},
                "statistics": {"accepted": 4},
                "artifact": artifact,
                "publication_gate": "blocked-fixture-rights-review",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    official = root / "official.csv"
    official.write_text(
        "cep,ibge,latitude,longitude,source\n"
        "01001000,3550308,-23.5505,-46.6333,official-fixture\n",
        encoding="utf-8",
    )
    return osm, official


def _build_fixture(root: Path) -> dict[str, Path]:
    source, gpkg = _write_inputs(root)
    boundaries = root / "boundaries.zip"
    write_municipality_boundaries(
        boundaries,
        [
            (
                "3550308",
                [(-47.0, -24.0), (-46.0, -24.0), (-46.0, -23.0), (-47.0, -23.0)],
            ),
            (
                "3304557",
                [(-44.0, -23.5), (-42.5, -23.5), (-42.5, -22.0), (-44.0, -22.0)],
            ),
        ],
    )
    source_lock = root / "source-lock.json"
    quality_policy = root / "quality-policy.json"
    _write_source_lock(source_lock, source, gpkg, boundaries)
    _write_quality_policy(quality_policy)
    osm, official = _write_validation_inputs(root, source_lock)
    database = root / "build.sqlite"
    normalized = root / "build.jsonl"
    build_manifest = root / "build.manifest.json"
    enrichment = Path("config/enrichment-v1.json").resolve()
    build_database(
        opencep_path=source,
        ibge_path=gpkg,
        output_path=database,
        export_path=normalized,
        manifest_path=build_manifest,
        source_lock_path=source_lock,
        osm_observations_path=osm,
        municipality_boundaries_path=boundaries,
        enrichment_config_path=enrichment,
        quality_config_path=quality_policy,
    )
    quality = build_quality_report(
        database_path=database,
        build_manifest_path=build_manifest,
        ibge_path=gpkg,
        osm_observations_path=osm,
        official_holdout_path=official,
        official_holdout_source_id="official-fixture-v1",
        municipality_boundaries_path=boundaries,
        enrichment_config_path=enrichment,
        quality_policy_path=quality_policy,
    )
    quality_report = root / "quality-report.json"
    write_quality_report(quality, quality_report)
    quality_markdown = root / "quality-report.md"
    quality_markdown.write_text(quality_report_markdown(quality), encoding="utf-8")
    notice = root / "NOTICE.md"
    notice.write_text(
        "OpenCEP fixture\nIBGE fixture\nOpenStreetMap fixture\n", encoding="utf-8"
    )
    return {
        "database_path": database,
        "normalized_path": normalized,
        "build_manifest_path": build_manifest,
        "quality_report_path": quality_report,
        "quality_markdown_path": quality_markdown,
        "notice_path": notice,
        "source_lock_path": source_lock,
        "enrichment_config_path": enrichment,
        "quality_policy_path": quality_policy,
        "ibge_path": gpkg,
        "osm_observations_path": osm,
        "official_holdout_path": official,
        "official_holdout_source_id": "official-fixture-v1",
        "municipality_boundaries_path": boundaries,
        "corrections_path": None,
    }


class ReleaseTests(unittest.TestCase):
    def test_verifier_accepts_maximum_bounded_provenance_field(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "large.csv"
            row = {column: "" for column in _CSV_COLUMNS}
            row.update(
                {
                    "cep": "01001000",
                    "geo_source": "x" * 2048,
                    "evidence_digest": "sha256:" + "0" * 64,
                    "dataset_version": "fixture-v1",
                }
            )
            with export.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=_CSV_COLUMNS, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerow(row)
            _verify_sorted_export(export, expected_records=1, file_format=_CSV_FORMAT)

    def test_packager_rejects_unbounded_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            connection = sqlite3.connect(inputs["database_path"])
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE cep_geo SET geo_source = ? WHERE cep = '01001000'",
                ("x" * 300000,),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "does not match build manifest"):
                package_release(**inputs, output_directory=root / "release")

    def test_packages_and_verifies_byte_identical_offline_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            packages = []
            for name in ("release-one", "release-two"):
                output = root / name
                package_release(**inputs, output_directory=output)
                packages.append(output)
                result = verify_release(output)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["record_count"], 3)

            first_hashes = {path.name: _sha256(path) for path in packages[0].iterdir()}
            second_hashes = {path.name: _sha256(path) for path in packages[1].iterdir()}
            self.assertEqual(first_hashes, second_hashes)

            database = packages[0] / "opencepgeo-fixture-release-v1.sqlite"
            known = lookup(database, "01001000")
            self.assertEqual(known["geo"]["coordinates"], [-46.6333, -23.5505])
            self.assertEqual(known["geo"]["precision"], "osm_postcode")
            unresolved = lookup(database, "53990959")
            self.assertIsNone(unresolved["geo"])
            self.assertNotIn("geo_source", unresolved)
            self.assertIsNone(lookup(database, "00000000"))

    def test_verifier_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            output = root / "release"
            package_release(**inputs, output_directory=output)
            with (output / "NOTICE.md").open("a", encoding="utf-8") as handle:
                handle.write("changed\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                verify_release(output)

    def test_packager_rejects_missing_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            inputs["notice_path"].write_text("IBGE fixture\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing attribution: OpenCEP"):
                package_release(**inputs, output_directory=root / "release")

    def test_packager_rejects_incompatible_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            connection = sqlite3.connect(inputs["database_path"])
            connection.execute(
                "UPDATE metadata SET value = 'future-schema' WHERE key = 'format'"
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "incompatible schema"):
                package_release(**inputs, output_directory=root / "release")

    def test_packager_rejects_row_count_regression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            connection = sqlite3.connect(inputs["database_path"])
            connection.execute("DELETE FROM cep_geo WHERE cep = '20010000'")
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(ValueError, "does not match build manifest"):
                package_release(**inputs, output_directory=root / "release")

    def test_packager_rejects_fabricated_incomplete_quality_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            inputs["quality_report_path"].write_text(
                json.dumps(
                    {
                        "format": "opencepgeo-quality-report-v2",
                        "dataset_version": "fixture-release-v1",
                        "status": "pass",
                        "inputs": {"database_sha256": _sha256(inputs["database_path"])},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "canonical recomputed report"):
                package_release(**inputs, output_directory=root / "release")

    def test_packager_rejects_arbitrary_quality_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            inputs["quality_markdown_path"].write_text(
                "# Fabricated PASS\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Markdown does not match"):
                package_release(**inputs, output_directory=root / "release")

    def test_packager_rejects_artifact_from_a_different_builder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            with mock.patch(
                "opencepgeo.release.builder_identity",
                return_value={
                    "name": "opencepgeo",
                    "version": "different",
                    "source_tree_sha256": "0" * 64,
                },
            ):
                with self.assertRaisesRegex(ValueError, "current builder"):
                    package_release(**inputs, output_directory=root / "release")

    def test_packager_rejects_wrong_municipality_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            changed = root / "changed-boundaries.zip"
            changed.write_bytes(
                inputs["municipality_boundaries_path"].read_bytes() + b"changed"
            )
            inputs["municipality_boundaries_path"] = changed
            with self.assertRaisesRegex(ValueError, "municipality boundaries"):
                package_release(**inputs, output_directory=root / "release")

    def test_export_verification_compares_rows_to_sqlite_semantically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = _build_fixture(root)
            output = root / "release"
            package_release(**inputs, output_directory=output)
            database = output / "opencepgeo-fixture-release-v1.sqlite"

            csv_path = output / "opencepgeo-fixture-release-v1.csv"
            with csv_path.open(encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            csv_rows[0]["city"] = "fabricated"
            changed_csv = root / "changed.csv"
            with changed_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=_CSV_COLUMNS, lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(csv_rows)
            with self.assertRaisesRegex(ValueError, "does not match SQLite"):
                _verify_sorted_export(
                    changed_csv,
                    expected_records=3,
                    file_format=_CSV_FORMAT,
                    database_path=database,
                )

            jsonl_path = output / "opencepgeo-fixture-release-v1.jsonl"
            jsonl_rows = [
                json.loads(line)
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            ]
            jsonl_rows[0]["city"] = "fabricated"
            changed_jsonl = root / "changed.jsonl"
            changed_jsonl.write_text(
                "".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in jsonl_rows
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not match SQLite"):
                _verify_sorted_export(
                    changed_jsonl,
                    expected_records=3,
                    file_format=_JSONL_FORMAT,
                    database_path=database,
                )


if __name__ == "__main__":
    unittest.main()
