import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from opencepgeo.refresh_policy import (
    RefreshPolicyError,
    enforce_refresh_policy,
    load_refresh_policy,
    natural_version_key,
    parse_instant,
    validate_operator_override,
)

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_normalized_build import (  # noqa: E402
    _write_correios_snapshot,
    _write_fixture,
    _classification_counts,
)

REPOSITORY = Path(__file__).resolve().parents[1]
POLICY_PATH = REPOSITORY / "config/refresh-policy-v1.json"


def _snapshot_verification(root: Path, row_count: int = 3, **kwargs):
    from opencepgeo.refresh_policy import verify_correios_snapshot

    directory = root / "snapshot"
    _write_correios_snapshot(directory, row_count, **kwargs)
    return directory, verify_correios_snapshot(directory)


def _claim(directory: Path) -> dict[str, object]:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    from tests.test_normalized_build import _sha256

    return {
        **manifest,
        "directory": directory.name,
        "manifest_sha256": _sha256(directory / "manifest.json"),
    }


class PolicyLoadingTests(unittest.TestCase):
    def test_repository_policy_loads_and_validates(self):
        policy = load_refresh_policy(POLICY_PATH)
        self.assertEqual(policy.document["format"], "opencepgeo-refresh-policy-v1")
        self.assertEqual(policy.naive_dnec_offset_minutes, -180)
        self.assertEqual(policy.maximum_capture_lag_days["weekly"], 45)
        self.assertEqual(policy.maximum_capture_lag_days["catch-up"], 730)
        self.assertEqual(policy.maximum_snapshot_age_days["weekly"], 14)
        self.assertEqual(policy.maximum_snapshot_age_days["catch-up"], 800)
        self.assertEqual(policy.retention_minimum_expiry_horizon_days, 180)

    def test_rejects_missing_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            del document["budgets"]["profiles"]["catch-up"]
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(RefreshPolicyError, "budget profiles"):
                load_refresh_policy(path)

    def test_rejects_fraction_above_one(self):
        with tempfile.TemporaryDirectory() as directory:
            document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            document["budgets"]["profiles"]["weekly"]["added"]["maximum_fraction"] = 1.5
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(RefreshPolicyError, "maximum_fraction"):
                load_refresh_policy(path)

    def test_rejects_retention_without_single_crawl_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            document = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
            document["retention"]["never_delete_missing_on_single_crawl"] = False
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(RefreshPolicyError, "single-crawl floor"):
                load_refresh_policy(path)


class TimestampParsingTests(unittest.TestCase):
    def test_accepts_utc_zulu_and_offsets(self):
        self.assertEqual(
            parse_instant("2026-08-11T23:49:30.843471Z"),
            datetime(2026, 8, 11, 23, 49, 30, 843471, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_instant("2026-08-11T20:49:30-03:00"),
            datetime(2026, 8, 11, 23, 49, 30, tzinfo=timezone.utc),
        )

    def test_accepts_naive_only_with_documented_offset(self):
        self.assertEqual(
            parse_instant("2026-07-31T09:18:52", allow_naive_at_offset=-180),
            datetime(2026, 7, 31, 12, 18, 52, tzinfo=timezone.utc),
        )

    def test_rejects_naive_without_offset(self):
        with self.assertRaisesRegex(RefreshPolicyError, "explicit UTC offset"):
            parse_instant("2026-07-31T09:18:52")

    def test_rejects_garbage(self):
        for bad in ("x", "", "2026-13-45", None, 12345, "not-a-date"):
            with self.assertRaises(RefreshPolicyError):
                parse_instant(bad)


class NaturalVersionOrderingTests(unittest.TestCase):
    def test_numeric_tokens_order_numerically(self):
        self.assertGreater(
            natural_version_key("2026.2.1-rc10"), natural_version_key("2026.2.1-rc9")
        )
        self.assertGreater(
            natural_version_key("2026.10.0"), natural_version_key("2026.9.0")
        )
        self.assertGreater(
            natural_version_key("2026.2.2"), natural_version_key("2026.2.1-rc3")
        )
        self.assertEqual(
            natural_version_key("2026.2.1"), natural_version_key("2026.2.1")
        )
        self.assertLess(
            natural_version_key("2026.2.1-rc3"), natural_version_key("2026.2.1")
        )


class SnapshotVerificationTests(unittest.TestCase):
    def test_real_shaped_snapshot_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            self.assertEqual(verification.record_count, 3)
            self.assertEqual(verification.raw_record_count, 3)
            self.assertEqual(verification.duplicate_record_count, 0)
            self.assertEqual(
                verification.cep_type_counts,
                {"1": 0, "2": 3, "3": 0, "4": 0, "5": 0, "6": 0},
            )
            claim = _claim(snapshot_dir)
            self.assertEqual(
                verification.addresses_identity["sha256"],
                claim["addresses_sha256"],
            )

    def test_tampered_manifest_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _snapshot_verification(root)
            manifest_path = root / "snapshot" / "manifest.json"
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["record_count"] += 1
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            from opencepgeo.refresh_policy import verify_correios_snapshot

            with self.assertRaisesRegex(
                RefreshPolicyError, "disagree with the actual bytes"
            ):
                verify_correios_snapshot(root / "snapshot")

    def test_tampered_addresses_fails_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _snapshot_verification(root)
            addresses = root / "snapshot" / "addresses.jsonl"
            payload = addresses.read_bytes()
            addresses.write_bytes(payload + payload.splitlines()[0] + b"\n")
            from opencepgeo.refresh_policy import verify_correios_snapshot

            with self.assertRaisesRegex(
                RefreshPolicyError,
                "not strictly increasing|actual bytes|hashes|artifact records",
            ):
                verify_correios_snapshot(root / "snapshot")

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _snapshot_verification(root)
            (root / "snapshot" / "raw-addresses.jsonl").unlink()
            from opencepgeo.refresh_policy import verify_correios_snapshot

            with self.assertRaisesRegex(RefreshPolicyError, "missing regular file"):
                verify_correios_snapshot(root / "snapshot")


class RefreshGateTests(unittest.TestCase):
    """Each acceptance-criterion rejection, proven against real gate inputs."""

    def setUp(self):
        self.policy = load_refresh_policy(POLICY_PATH)
        self.build_instant = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        self.classifications = {
            "added": 10,
            "address_changed": 5,
            "ibge_changed": 1,
            "missing_from_source": 2,
            "source_link_conflict": 0,
            "unchanged": 982,
        }

    def _gate(
        self,
        directory: Path,
        verification,
        *,
        profile="weekly",
        inherited_snapshot=None,
        classifications=None,
        override=None,
        dataset_version="2026.2.1-rc4",
        inherited_version="2026.2.1-rc3",
        inherited_count=1000,
    ):
        return enforce_refresh_policy(
            policy=self.policy,
            profile=profile,
            correios_claim=_claim(directory),
            snapshot_verification=verification,
            dataset_version=dataset_version,
            classification_counts=classifications or self.classifications,
            inherited_dataset_version=inherited_version,
            inherited_snapshot=inherited_snapshot,
            inherited_record_count=inherited_count,
            build_instant=self.build_instant,
            override_reason=override,
        )

    def test_compliant_weekly_refresh_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            report = self._gate(snapshot_dir, verification)
            self.assertEqual(report.profile, "weekly")
            self.assertEqual(report.captured_at_utc, "2026-08-11T01:30:00Z")
            # 2026-08-11T00:00:00Z naive → wait: fixture uses Z here; lag 1.5h.
            self.assertAlmostEqual(report.capture_lag_days, 0.0625, places=2)
            self.assertIsNone(report.override)

    def test_stale_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = (self.build_instant - timedelta(days=30)).isoformat()
            published = (self.build_instant - timedelta(days=31)).isoformat()
            snapshot_dir, verification = _snapshot_verification(
                root, captured_at=captured, dnec_published_at=published
            )
            with self.assertRaisesRegex(RefreshPolicyError, "age exceeds the weekly"):
                self._gate(snapshot_dir, verification)

    def test_stale_snapshot_allowed_under_catch_up_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = (self.build_instant - timedelta(days=30)).isoformat()
            published = (self.build_instant - timedelta(days=31)).isoformat()
            snapshot_dir, verification = _snapshot_verification(
                root, captured_at=captured, dnec_published_at=published
            )
            report = self._gate(snapshot_dir, verification, profile="catch-up")
            self.assertEqual(report.profile, "catch-up")

    def test_excessive_capture_lag_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = (self.build_instant - timedelta(days=50)).isoformat()
            published = (self.build_instant - timedelta(days=120)).isoformat()
            snapshot_dir, verification = _snapshot_verification(
                root, captured_at=captured, dnec_published_at=published
            )
            with self.assertRaisesRegex(RefreshPolicyError, "capture lag exceeds"):
                self._gate(snapshot_dir, verification)

    def test_capture_before_publication_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = (self.build_instant - timedelta(days=3)).isoformat()
            published = (self.build_instant - timedelta(days=2)).isoformat()
            snapshot_dir, verification = _snapshot_verification(
                root, captured_at=captured, dnec_published_at=published
            )
            with self.assertRaisesRegex(
                RefreshPolicyError, "captured before its DNEC publication"
            ):
                self._gate(snapshot_dir, verification)

    def test_garbage_timestamps_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(
                root, captured_at="yesterday", dnec_published_at="x"
            )
            with self.assertRaisesRegex(RefreshPolicyError, "ISO-8601"):
                self._gate(snapshot_dir, verification)

    def test_naive_captured_at_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(
                root,
                captured_at="2026-08-11T01:30:00",
                dnec_published_at="2026-08-11T00:00:00Z",
            )
            with self.assertRaisesRegex(RefreshPolicyError, "explicit UTC offset"):
                self._gate(snapshot_dir, verification)

    def test_naive_dnec_accepted_under_documented_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(
                root,
                captured_at="2026-08-11T04:30:00Z",
                dnec_published_at="2026-08-11T01:30:00",
            )
            report = self._gate(snapshot_dir, verification)
            # naive 01:30 at -03:00 → 04:30Z, lag exactly 0 days.
            self.assertEqual(report.dnec_published_at_utc, "2026-08-11T04:30:00Z")
            self.assertEqual(report.capture_lag_days, 0.0)

    def test_out_of_order_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            inherited = {
                "dnec_published_at": "2026-12-01T00:00:00Z",
                "dnec_timezone_semantics": "unspecified_by_source",
                "captured_at": "2026-12-02T00:00:00Z",
                "addresses_sha256": "0" * 64,
                "manifest_sha256": "1" * 64,
            }
            with self.assertRaisesRegex(RefreshPolicyError, "does not advance"):
                self._gate(snapshot_dir, verification, inherited_snapshot=inherited)

    def test_replayed_publication_identity_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            claim = _claim(snapshot_dir)
            inherited = {
                "dnec_published_at": "2026-01-01T00:00:00Z",
                "dnec_timezone_semantics": "unspecified_by_source",
                "captured_at": "2026-01-02T00:00:00Z",
                "addresses_sha256": claim["addresses_sha256"],
                "manifest_sha256": claim["manifest_sha256"],
            }
            with self.assertRaisesRegex(
                RefreshPolicyError, "reuses the inherited release's publication"
            ):
                self._gate(snapshot_dir, verification, inherited_snapshot=inherited)

    def test_dataset_version_regression_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            with self.assertRaisesRegex(RefreshPolicyError, "does not progress past"):
                self._gate(
                    snapshot_dir,
                    verification,
                    dataset_version="2026.2.1-rc3",
                    inherited_version="2026.2.1-rc3",
                )

    def test_over_budget_delta_rejected_weekly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            classifications = {
                "added": 500,  # 50% of 1000 > 2% weekly, within 50% catch-up
                "address_changed": 5,
                "ibge_changed": 1,
                "missing_from_source": 2,
                "source_link_conflict": 0,
                "unchanged": 492,
            }
            with self.assertRaisesRegex(
                RefreshPolicyError, "change budget exceeded under the weekly profile"
            ):
                self._gate(
                    snapshot_dir,
                    verification,
                    classifications=classifications,
                )

    def test_same_delta_passes_catch_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            classifications = {
                "added": 500,
                "address_changed": 5,
                "ibge_changed": 1,
                "missing_from_source": 2,
                "source_link_conflict": 0,
                "unchanged": 492,
            }
            report = self._gate(
                snapshot_dir,
                verification,
                profile="catch-up",
                classifications=classifications,
            )
            self.assertIsNone(report.override)
            self.assertEqual(report.budgets["added"]["observed"], 500)

    def test_rc3_calibrated_deltas_classified_correctly(self):
        # RC3 (the calibration reference) must fail weekly and pass catch-up.
        rc3 = {
            "added": 411519,
            "address_changed": 115131,
            "ibge_changed": 53,
            "missing_from_source": 21771,
            "source_link_conflict": 6,
            "unchanged": 1072353,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            with self.assertRaisesRegex(
                RefreshPolicyError, "change budget exceeded under the weekly profile"
            ):
                self._gate(
                    snapshot_dir,
                    verification,
                    classifications=rc3,
                    inherited_count=1209314,
                )
            report = self._gate(
                snapshot_dir,
                verification,
                profile="catch-up",
                classifications=rc3,
                inherited_count=1209314,
            )
            self.assertAlmostEqual(
                report.budgets["added"]["fraction"], 0.34029127, places=6
            )

    def test_override_records_reason_and_breached_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_dir, verification = _snapshot_verification(root)
            classifications = {
                "added": 500,
                "address_changed": 5,
                "ibge_changed": 1,
                "missing_from_source": 2,
                "source_link_conflict": 0,
                "unchanged": 492,
            }
            report = self._gate(
                snapshot_dir,
                verification,
                classifications=classifications,
                override="authorised backlog import (ticket PIN-217 calibration)",
            )
            self.assertIsNotNone(report.override)
            self.assertIn("added", report.override["breached_metrics"])
            self.assertIn("backlog import", str(report.override["reason"]))

    def test_freshness_never_overridable(self):
        # A stale snapshot with a budget override still fails: only budget
        # breaches honor the override.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = (self.build_instant - timedelta(days=30)).isoformat()
            published = (self.build_instant - timedelta(days=31)).isoformat()
            snapshot_dir, verification = _snapshot_verification(
                root, captured_at=captured, dnec_published_at=published
            )
            with self.assertRaisesRegex(RefreshPolicyError, "age exceeds"):
                self._gate(
                    snapshot_dir,
                    verification,
                    override="operator said fine",
                )


class OperatorOverrideValidationTests(unittest.TestCase):
    def test_requires_non_empty(self):
        for bad in (None, "", "   "):
            with self.assertRaisesRegex(RefreshPolicyError, "non-empty"):
                validate_operator_override(bad)

    def test_rejects_oversized(self):
        with self.assertRaisesRegex(RefreshPolicyError, "512"):
            validate_operator_override("x" * 513)


class BuilderIntegrationTests(unittest.TestCase):
    """End-to-end through build_database_from_normalized's gate wiring."""

    def test_build_records_gate_report_and_freshness_metadata(self):
        from opencepgeo.database import build_database_from_normalized

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            output = root / "out.sqlite"
            build_database_from_normalized(
                **arguments,
                output_path=output,
                normalized_output_path=root / "out.jsonl",
                manifest_path=root / "out.manifest.json",
                build_instant=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
            )
            manifest = json.loads(
                (root / "out.manifest.json").read_text(encoding="utf-8")
            )
            policy_block = manifest["inputs"]["normalized_refresh"]["refresh_policy"]
            self.assertEqual(policy_block["profile"], "catch-up")
            self.assertIn("gate_report", policy_block)
            self.assertEqual(
                policy_block["gate_report"]["captured_at_utc"], "2026-08-11T01:30:00Z"
            )
            self.assertIsNotNone(policy_block["correios_snapshot_verified"])
            override = policy_block["gate_report"]["override"]
            self.assertIn("added", override["breached_metrics"])
            import sqlite3

            metadata = dict(
                sqlite3.connect(output).execute("SELECT key, value FROM metadata")
            )
            self.assertEqual(metadata["dnec_published_at"], "2026-08-11T00:00:00Z")
            self.assertEqual(metadata["captured_at"], "2026-08-11T01:30:00Z")
            self.assertEqual(metadata["refresh_profile"], "catch-up")
            self.assertIn("refresh_policy_sha256", metadata)

    def test_stale_snapshot_fails_the_build(self):
        from opencepgeo.database import build_database_from_normalized

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(
                root,
                snapshot_captured_at="2026-01-01T01:30:00Z",
                snapshot_dnec_published_at="2026-01-01T00:00:00Z",
            )
            arguments["refresh_profile"] = "weekly"
            with self.assertRaisesRegex(RefreshPolicyError, "age exceeds"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                    build_instant=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
                )

    def test_tampered_snapshot_fails_the_build(self):
        from opencepgeo.database import build_database_from_normalized

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            addresses = arguments["correios_snapshot_path"] / "addresses.jsonl"
            lines = addresses.read_bytes().splitlines(keepends=True)
            addresses.write_bytes(b"".join(lines[:-1]))  # drop one row
            with self.assertRaisesRegex(RefreshPolicyError, "snapshot manifest hashes|artifact records"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                    build_instant=datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
                )

    def test_missing_snapshot_directory_fails_the_build(self):
        from opencepgeo.database import build_database_from_normalized

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            arguments["correios_snapshot_path"] = root / "not-there"
            with self.assertRaisesRegex(
                (RefreshPolicyError, ValueError), "basename must match"
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                    build_instant=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
                )

    def test_expired_retention_fails_the_build(self):
        from opencepgeo.database import build_database_from_normalized

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            diff_path = arguments["refresh_diff_path"]
            records = [json.loads(line) for line in diff_path.read_text().splitlines()]
            # Mark the last row as missing_from_source with an expiry far in
            # the past relative to the 2026-08-11 capture. For the refresh
            # manifest's cross-checks to stay consistent, the vanished CEP
            # also disappears from the Correios snapshot (that is what
            # missing_from_source means), so rebuild the snapshot with one
            # fewer address and rebind every claim to the new bytes.
            records[-1]["classification"] = "missing_from_source"
            records[-1]["geography_action"] = "retained_missing"
            records[-1]["valid_until"] = "2020-01-01"
            diff_path.write_bytes(
                b"".join(
                    json.dumps(r, sort_keys=True, separators=(",", ":")).encode()
                    + b"\n"
                    for r in records
                )
            )
            from tests.test_normalized_build import _sha256, _write_correios_snapshot

            snapshot_directory = arguments["correios_snapshot_path"]
            _write_correios_snapshot(snapshot_directory, 2)
            manifest_path = snapshot_directory / "manifest.json"
            snapshot_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            refresh_path = arguments["refresh_manifest_path"]
            refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
            refresh["artifacts"][diff_path.name].update(
                bytes=diff_path.stat().st_size, sha256=_sha256(diff_path)
            )
            refresh["classification_counts"]["added"] -= 1
            refresh["classification_counts"]["missing_from_source"] += 1
            refresh["inputs"]["correios_snapshot"] = {
                key: value
                for key, value in snapshot_manifest.items()
                if key != "artifacts"
            } | {
                "directory": snapshot_directory.name,
                "manifest_sha256": _sha256(manifest_path),
            }
            # The inherited base now legitimately owned 2 rows (one still
            # unchanged, one vanished from source), so its record count — and
            # the current input it mirrors — must say so for the cross-checks.
            refresh["inputs"]["current_opencepgeo"]["record_count"] = 2
            refresh["inherited_base_release"]["record_count"] = 2
            _write_json(refresh_path, refresh)
            quality_path = arguments["refresh_quality_path"]
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            quality["classification_counts"] = dict(refresh["classification_counts"])
            quality["correios_snapshot"] = {
                key: value
                for key, value in refresh["inputs"]["correios_snapshot"].items()
                if key not in {"directory", "manifest_sha256", "addresses_sha256"}
            }
            quality["geography_action_counts"]["unresolved"] -= 1
            quality["geography_action_counts"]["retained_missing"] += 1
            quality["inherited_base_release"]["record_count"] = 2
            _write_json(quality_path, quality)
            refresh["artifacts"][quality_path.name].update(
                bytes=quality_path.stat().st_size, sha256=_sha256(quality_path)
            )
            _write_json(refresh_path, refresh)
            with self.assertRaisesRegex(
                (RefreshPolicyError, ValueError),
                "expired before the snapshot capture date",
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                    build_instant=datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc),
                )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
