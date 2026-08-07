import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from opencepgeo.config import EnrichmentConfig
from opencepgeo.database import build_database
from opencepgeo.model import MunicipalityReference, Observation, Point
from opencepgeo.quality import (
    _evidence_selection_statistics,
    _gated_cohort_inputs,
    _production_eligible_osm,
    build_quality_report,
    load_quality_policy,
    split_holdout,
)


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


def _bucket(source: str) -> int:
    return int.from_bytes(hashlib.sha256(source.encode()).digest()[:8], "big") % 2


def _point_for_bucket(cep: str, wanted: int, base: tuple[float, float]):
    for index in range(100):
        latitude = base[0] + index / 10000
        longitude = base[1]
        source = f"openstreetmap:node/{cep}{index}"
        if _bucket(source) == wanted:
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
                "format": "opencepgeo-quality-policy-v2",
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
                            "required_address_classes": [
                                "urban_address_proxy",
                                "rural_or_general_address_proxy",
                            ],
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
                        "sp_osm": {
                            "cohort": "leave_observation_out",
                            "ufs": ["SP"],
                            "allowed_precision_tiers": ["osm_postcode"],
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
            }
        ),
        encoding="utf-8",
    )


class QualityTests(unittest.TestCase):
    def test_gated_cohorts_use_distinct_evidence_scopes(self):
        polygon_only = Observation("01001000", Point(0.0, 0.0, "polygon-only"))
        retained = Observation("02002000", Point(0.0, 0.0, "retained"))
        inputs = _gated_cohort_inputs(
            [retained, polygon_only],
            [retained],
        )
        self.assertEqual(inputs["leave_observation_out"], [retained])
        self.assertEqual(inputs["unseen_cep"], [retained, polygon_only])

    def test_repository_quality_sample_floors_and_rr_scopes_are_explicit(self):
        policy = load_quality_policy("config/quality-v1.json")
        cohorts = policy.validation["cohorts"]
        purposes = policy.validation["purposes"]
        per_uf = policy.validation["per_uf"]
        self.assertEqual(cohorts["leave_observation_out"]["minimum_records"], 75000)
        self.assertEqual(
            purposes["nearby_store_same_cep_proxy"]["minimum_records"], 75000
        )
        self.assertNotIn("RR", per_uf["required_ufs"])
        self.assertEqual(
            purposes["rr_osm_postcode_same_cep"],
            {
                "cohort": "leave_observation_out",
                "ufs": ["RR"],
                "allowed_precision_tiers": ["osm_postcode"],
                "minimum_records": 10,
                "maximum_p95_km": 2.0,
                "rationale": "RR production-retained OSM-tier predictions retain the same strict same-CEP consistency ceiling as the national OSM tier; this is not a positional-accuracy claim.",
            },
        )
        self.assertEqual(
            purposes["rr_municipality_coarse_address_exception"]["minimum_records"],
            50,
        )
        self.assertEqual(
            purposes["rr_municipality_coarse_address_exception"]["maximum_p95_km"],
            150.0,
        )

    def test_gated_evidence_matches_estimator_outlier_and_radius_rules(self):
        def observation(cep: str, latitude: float, identity: str) -> Observation:
            return Observation(
                cep,
                Point(latitude, 0.0, identity, identity),
            )

        observations = [
            observation("01001000", 0.0, "cluster-1"),
            observation("01001000", 0.001, "cluster-2"),
            observation("01001000", -0.001, "cluster-3"),
            observation("01001000", 1.0, "outlier"),
            observation("02002000", 0.0, "wide-1"),
            observation("02002000", 0.1, "wide-2"),
        ]
        metadata = {
            cep: ("3550308", "SP", None, None, None, None, None)
            for cep in ("01001000", "02002000")
        }
        municipality = MunicipalityReference(
            Point(0.0, 0.0, "ibge"),
            1,
            0.0,
            "sha256:" + "0" * 64,
        )
        enrichment = EnrichmentConfig(
            version="fixture",
            min_prefix_samples=3,
            max_prefix_radius_km=25.0,
            max_observed_radius_km=10.0,
            max_osm_radius_km=5.0,
            max_osm_municipality_distance_km=250.0,
            outlier_min_samples=3,
            outlier_mad_multiplier=3.0,
            outlier_floor_km=0.25,
        )

        eligible, excluded = _production_eligible_osm(
            observations,
            metadata,
            {"3550308": municipality},
            enrichment,
        )

        self.assertEqual(
            [observation.point.evidence_id for observation in eligible],
            ["cluster-1", "cluster-2", "cluster-3"],
        )
        self.assertEqual(excluded["robust_spatial_outlier"], 1)
        self.assertEqual(excluded["cep_group_radius_rejection"], 2)

    def test_unknown_ceps_cannot_dilute_outside_fraction(self):
        statistics = _evidence_selection_statistics(
            total=102,
            polygon_eligible=101,
            interior=1,
            boundary=0,
            outside=1,
            unknown=100,
        )
        self.assertEqual(statistics["known_target_observations"], 2)
        self.assertEqual(statistics["polygon_eligible_observations"], 101)
        self.assertEqual(statistics["outside_target_municipality_fraction"], 0.5)
        self.assertEqual(
            statistics["unknown_cep_observations_retained_for_missingness"], 100
        )

    def test_unseen_cep_split_never_leaks_a_heldout_cep_into_training(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "quality.json"
            _write_policy(policy_path)
            policy = load_quality_policy(policy_path)
            observations = []
            for cep_index in range(20):
                cep = f"{cep_index:08d}"
                for sample_index in range(2):
                    evidence_index = cep_index * 2 + sample_index
                    observations.append(
                        Observation(
                            cep=cep,
                            ibge=None,
                            point=Point(
                                latitude=-23.5 + evidence_index / 1000,
                                longitude=-46.6,
                                source=f"openstreetmap:node/{evidence_index}",
                                evidence_id=f"openstreetmap:node/{evidence_index}",
                            ),
                        )
                    )

            training, heldout = split_holdout(
                observations, policy, group_by_cep=True
            )

            self.assertTrue(training)
            self.assertTrue(heldout)
            self.assertTrue(
                {observation.cep for observation in training}.isdisjoint(
                    observation.cep for observation in heldout
                )
            )

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
            sp_train = _point_for_bucket("01001000", 1, (-23.55, -46.63))
            sp_holdout = _point_for_bucket("01001000", 0, (-23.55, -46.63))
            rj_train = _point_for_bucket("20010000", 1, (-22.91, -43.20))
            rj_holdout = _point_for_bucket("20010000", 0, (-22.91, -43.20))
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
                        "latitude": rj_train[0],
                        "longitude": rj_train[1],
                        "source": rj_train[2],
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
            manifest = root / "artifact.manifest.json"
            build_database(
                opencep_path=source,
                ibge_path=gpkg,
                osm_observations_path=osm,
                output_path=database,
                manifest_path=manifest,
                enrichment_config_path=enrichment,
                quality_config_path=policy,
                source_version="fixture-v1",
            )
            arguments = {
                "database_path": database,
                "build_manifest_path": manifest,
                "ibge_path": gpkg,
                "osm_observations_path": osm,
                "official_holdout_path": official,
                "official_holdout_source_id": "official-fixture-v1",
                "enrichment_config_path": enrichment,
                "quality_policy_path": policy,
            }
            first = build_quality_report(**arguments)
            second = build_quality_report(**arguments)
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "pass")
            self.assertEqual(
                first["cohorts"]["leave_observation_out"]["metrics"]
                ["precision:osm_postcode"]["count"],
                2,
            )
            self.assertEqual(
                first["cohorts"]["unseen_cep"]["metrics"]
                ["precision:municipality"]["count"],
                4,
            )
            self.assertEqual(first["official_pilot"]["evaluated_observations"], 1)
            self.assertEqual(first["purposes"]["sp_osm"]["ufs"], ["SP"])
            self.assertEqual(first["purposes"]["sp_osm"]["count"], 1)

            changed_osm = root / "changed-osm.csv"
            changed_osm.write_bytes(osm.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "OSM evidence"):
                build_quality_report(
                    **{**arguments, "osm_observations_path": changed_osm}
                )

            changed_ibge = root / "changed-ibge.gpkg"
            changed_ibge.write_bytes(gpkg.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "IBGE input"):
                build_quality_report(**{**arguments, "ibge_path": changed_ibge})

            changed_manifest = root / "changed.manifest.json"
            changed_document = json.loads(manifest.read_text(encoding="utf-8"))
            changed_document["dataset_version"] = "changed"
            changed_manifest.write_text(json.dumps(changed_document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dataset version mismatch"):
                build_quality_report(
                    **{**arguments, "build_manifest_path": changed_manifest}
                )

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
