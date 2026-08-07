import unittest

from opencepgeo.estimator import CentroidEstimator, normalize_cep, normalize_ibge
from opencepgeo.model import Observation, Point


class NormalizationTests(unittest.TestCase):
    def test_normalizes_formatted_cep(self):
        self.assertEqual(normalize_cep("01001-000"), "01001000")
        self.assertIsNone(normalize_cep("01001"))

    def test_requires_seven_digit_ibge(self):
        self.assertEqual(normalize_ibge("3550308"), "3550308")
        self.assertIsNone(normalize_ibge("355030"))


class EstimatorTests(unittest.TestCase):
    def setUp(self):
        self.municipalities = {"3550308": Point(-23.5505, -46.6333, "ibge-localidades")}

    def test_exact_observation_wins(self):
        estimator = CentroidEstimator(
            [Observation("01001000", Point(-23.55, -46.63, "store"), "3550308")],
            self.municipalities,
        )
        estimate = estimator.estimate("01001000", "3550308")
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate.precision, "observed_cep")
        self.assertEqual(estimate.sample_size, 1)
        self.assertEqual(estimate.method, "robust_median_first_party")

    def test_prefix_requires_same_municipality_and_minimum_samples(self):
        observations = [
            Observation(
                f"01001{suffix:03d}",
                Point(-23.55, -46.63 + suffix / 10000, f"store:{suffix}"),
                "3550308",
            )
            for suffix in range(3)
        ]
        estimator = CentroidEstimator(observations, self.municipalities)
        estimate = estimator.estimate("01001999", "3550308")
        self.assertEqual(estimate.precision, "observed_cep_prefix")
        self.assertEqual(estimate.sample_size, 3)

    def test_rejects_spatially_wide_prefix(self):
        observations = [
            Observation("01001001", Point(-23.55, -46.63, "store:1"), "3550308"),
            Observation("01001002", Point(-22.90, -43.20, "store:2"), "3550308"),
            Observation("01001003", Point(-15.78, -47.90, "store:3"), "3550308"),
        ]
        estimator = CentroidEstimator(observations, self.municipalities)
        estimate = estimator.estimate("01001999", "3550308")
        self.assertEqual(estimate.precision, "municipality")

    def test_osm_point_outside_municipality_distance_fails_closed(self):
        osm = [
            Observation("01001000", Point(-3.1, -60.0, "openstreetmap:node/1"))
        ]
        estimate = CentroidEstimator(
            [],
            self.municipalities,
            osm_observations=osm,
            max_osm_municipality_distance_km=100.0,
        ).estimate("01001000", "3550308")
        self.assertEqual(estimate.precision, "municipality")

    def test_duplicate_evidence_identity_is_deduplicated(self):
        point = Point(-23.55, -46.63, "store:1")
        observations = [
            Observation("01001001", point, "3550308"),
            Observation("01001001", point, "3550308"),
            Observation("01001002", Point(-23.55, -46.631, "store:2"), "3550308"),
            Observation("01001003", Point(-23.55, -46.632, "store:3"), "3550308"),
        ]
        estimate = CentroidEstimator(observations, self.municipalities).estimate(
            "01001999", "3550308"
        )
        self.assertEqual(estimate.evidence_count, 3)

    def test_conflicting_evidence_identity_is_rejected(self):
        observations = [
            Observation("01001000", Point(-23.55, -46.63, "store:1"), "3550308"),
            Observation("01001000", Point(-23.56, -46.64, "store:1"), "3550308"),
        ]
        with self.assertRaisesRegex(ValueError, "conflicting duplicate evidence"):
            CentroidEstimator(observations, self.municipalities).estimate(
                "01001000", "3550308"
            )

    def test_first_party_ibge_conflict_is_rejected(self):
        estimator = CentroidEstimator(
            [Observation("01001000", Point(-23.55, -46.63, "store:1"), "3304557")],
            self.municipalities,
        )
        with self.assertRaisesRegex(ValueError, "IBGE conflicts"):
            estimator.estimate("01001000", "3550308")

    def test_municipality_is_last_resort(self):
        estimator = CentroidEstimator([], self.municipalities)
        estimate = estimator.estimate("01001999", "3550308")
        self.assertEqual(estimate.precision, "municipality")
        self.assertEqual(estimate.sources, ("ibge-localidades",))

    def test_osm_postcode_is_below_first_party_and_above_prefix(self):
        first_party = [
            Observation("01001000", Point(-23.55, -46.63, "store"), "3550308")
        ]
        osm = [
            Observation(
                "01001000",
                Point(-23.551, -46.631, "openstreetmap:node/1"),
            )
        ]
        estimator = CentroidEstimator(
            first_party, self.municipalities, osm_observations=osm
        )
        self.assertEqual(
            estimator.estimate("01001000", "3550308").precision, "observed_cep"
        )

        estimator = CentroidEstimator([], self.municipalities, osm_observations=osm)
        estimate = estimator.estimate("01001000", "3550308")
        self.assertEqual(estimate.precision, "osm_postcode")
        self.assertEqual(estimate.method, "robust_median_osm_postcode")
        self.assertEqual(estimate.sources, ("openstreetmap",))
        self.assertRegex(estimate.evidence_digest, r"^sha256:[0-9a-f]{64}$")

    def test_robust_filter_rejects_exact_outlier(self):
        observations = [
            Observation("01001000", Point(-23.5500, -46.6300, "a"), "3550308"),
            Observation("01001000", Point(-23.5501, -46.6301, "b"), "3550308"),
            Observation("01001000", Point(-23.5499, -46.6299, "c"), "3550308"),
            Observation("01001000", Point(-3.1, -60.0, "outlier"), "3550308"),
        ]
        estimate = CentroidEstimator(observations, self.municipalities).estimate(
            "01001000", "3550308"
        )
        self.assertEqual(estimate.precision, "observed_cep")
        self.assertEqual(estimate.evidence_count, 3)
        self.assertNotIn("outlier", estimate.sources)

    def test_two_excessively_spread_exact_points_are_rejected(self):
        observations = [
            Observation("01001000", Point(-23.55, -46.63, "a"), "3550308"),
            Observation("01001000", Point(-3.1, -60.0, "b"), "3550308"),
        ]
        estimate = CentroidEstimator(observations, self.municipalities).estimate(
            "01001000", "3550308"
        )
        self.assertEqual(estimate.precision, "municipality")

    def test_excessively_spread_osm_postcode_points_are_rejected(self):
        osm = [
            Observation("01001000", Point(-23.55, -46.63, "openstreetmap:node/1")),
            Observation("01001000", Point(-22.90, -43.20, "openstreetmap:node/2")),
        ]
        estimate = CentroidEstimator(
            [], self.municipalities, osm_observations=osm
        ).estimate("01001000", "3550308")
        self.assertEqual(estimate.precision, "osm_postcode")
        self.assertEqual(estimate.evidence_count, 1)

    def test_cross_municipality_points_do_not_satisfy_prefix_minimum(self):
        observations = [
            Observation("01001001", Point(-23.55, -46.63, "a"), "3550308"),
            Observation("01001002", Point(-23.55, -46.63, "b"), "3550308"),
            Observation("01001003", Point(-23.55, -46.63, "c"), "3304557"),
        ]
        estimate = CentroidEstimator(observations, self.municipalities).estimate(
            "01001999", "3550308"
        )
        self.assertEqual(estimate.precision, "municipality")


if __name__ == "__main__":
    unittest.main()
