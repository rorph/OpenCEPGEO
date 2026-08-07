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
        self.municipalities = {
            "3550308": Point(-23.5505, -46.6333, "ibge-localidades")
        }

    def test_exact_observation_wins(self):
        estimator = CentroidEstimator(
            [Observation("01001000", Point(-23.55, -46.63, "store"), "3550308")],
            self.municipalities,
        )
        estimate = estimator.estimate("01001000", "3550308")
        self.assertIsNotNone(estimate)
        self.assertEqual(estimate.precision, "observed_cep")
        self.assertEqual(estimate.sample_size, 1)

    def test_prefix_requires_same_municipality_and_minimum_samples(self):
        observations = [
            Observation(f"01001{suffix:03d}", Point(-23.55, -46.63 + suffix / 10000, "store"), "3550308")
            for suffix in range(3)
        ]
        estimator = CentroidEstimator(observations, self.municipalities)
        estimate = estimator.estimate("01001999", "3550308")
        self.assertEqual(estimate.precision, "observed_cep_prefix")
        self.assertEqual(estimate.sample_size, 3)

    def test_rejects_spatially_wide_prefix(self):
        observations = [
            Observation("01001001", Point(-23.55, -46.63, "store"), "3550308"),
            Observation("01001002", Point(-22.90, -43.20, "store"), "3550308"),
            Observation("01001003", Point(-15.78, -47.90, "store"), "3550308"),
        ]
        estimator = CentroidEstimator(observations, self.municipalities)
        estimate = estimator.estimate("01001999", "3550308")
        self.assertEqual(estimate.precision, "municipality")

    def test_municipality_is_last_resort(self):
        estimator = CentroidEstimator([], self.municipalities)
        estimate = estimator.estimate("01001999", "3550308")
        self.assertEqual(estimate.precision, "municipality")
        self.assertEqual(estimate.sources, ("ibge-localidades",))


if __name__ == "__main__":
    unittest.main()

