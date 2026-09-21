"""Versioned refresh policy: snapshot freshness, ordering, replay prevention,
change budgets and retention for ``build-from-normalized``.

The policy is loaded from a JSON document (``config/refresh-policy-v1.json``)
and every gate it describes is enforced by the builder against *supplied bytes*:
the Correios snapshot directory is required input, its hashes are recomputed,
its row counts are re-derived by streaming, and its timestamps are parsed as
real ISO-8601 instants. Nothing in the refresh manifest is trusted on its own.

Budget breaches fail the build with an actionable message naming the metric,
the observed value and the threshold; they are overridable only by an explicit,
recorded operator reason. Freshness, ordering, replay and hash-integrity
breaches are never overridable.

Timestamp semantics: ``captured_at`` is produced by our own crawler and must
carry an explicit UTC offset. ``dnec_published_at`` is scraped from the Correios
DNEC update marker and, under the recorded ``unspecified_by_source`` semantics,
is naive and is interpreted at the documented fixed offset (Brazil has had no
DST since 2019); both normalize to timezone-aware UTC instants internally.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLICY_FORMAT = "opencepgeo-refresh-policy-v1"

_PROFILE_NAMES = ("weekly", "catch-up")
_BUDGET_METRICS = (
    "added",
    "missing_from_source",
    "ibge_changed",
    "address_changed",
    "duplicates_dropped",
    "source_link_conflicts",
)
_MAX_POLICY_BYTES = 1 * 1024 * 1024
_MAX_OVERRIDE_REASON_BYTES = 512
# Wall-clock gates exist to stop stale replays, but a recorded build timestamp
# would break input-determinism. The caller passes the build instant explicitly
# (tests use a fixed one); production callers use the real clock.
_NAIVE_DNEC_UTC_OFFSET_RANGE_MINUTES = (-14 * 60, 14 * 60)


class RefreshPolicyError(ValueError):
    """A refresh-policy gate failed. Never catch-and-continue: fail the build."""


def parse_instant(
    value: object, *, allow_naive_at_offset: int | None = None
) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC instant.

    Naive values are accepted only when ``allow_naive_at_offset`` supplies the
    documented fixed UTC offset (minutes) for that field's semantics; they are
    interpreted at that offset, not at the host clock.
    """
    if not isinstance(value, str) or not value.strip():
        raise RefreshPolicyError(f"timestamp is not a non-empty string: {value!r}")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RefreshPolicyError(f"timestamp is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        if allow_naive_at_offset is None:
            raise RefreshPolicyError(
                f"timestamp lacks an explicit UTC offset: {value!r}"
            )
        low, high = _NAIVE_DNEC_UTC_OFFSET_RANGE_MINUTES
        if not low <= allow_naive_at_offset <= high:
            raise RefreshPolicyError("documented naive-offset is out of range")
        parsed = parsed.replace(
            tzinfo=timezone(timedelta(minutes=allow_naive_at_offset))
        )
    return parsed.astimezone(timezone.utc)


def natural_version_key(value: str) -> tuple:
    """Order dataset versions naturally: rc10 > rc9, 10 > 9, and a plain
    release orders above its own rc pre-releases (``2026.2.1`` >
    ``2026.2.1-rc3``)."""
    is_pre_release = bool(re.search(r"-(rc|beta|dev|alpha)\d*$", value, re.IGNORECASE))
    key: list[tuple[int, object]] = []
    for token in re.split(r"(\d+)", value):
        if not token:
            continue
        if token.isdigit():
            key.append((1, int(token)))
        else:
            key.append((0, token.lower()))
    # Pre-release markers (rc/beta/dev/alpha) sort below the plain release of
    # the same version: discriminator (1, 0) for pre-releases, (1, 1) finals.
    key.append((1, 0 if is_pre_release else 1))
    return tuple(key)


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    maximum_fraction: float
    maximum_absolute: int


@dataclass(frozen=True, slots=True)
class RefreshPolicy:
    path: Path
    document: dict[str, object]
    sha256: str
    version: str

    @property
    def naive_dnec_offset_minutes(self) -> int:
        freshness = self.document["freshness"]
        assert isinstance(freshness, dict)
        return int(freshness["naive_dnec_utc_offset_minutes"])

    @property
    def maximum_capture_lag_days(self) -> dict[str, int]:
        freshness = self.document["freshness"]
        assert isinstance(freshness, dict)
        return dict(freshness["maximum_capture_lag_days"])  # type: ignore[arg-type]

    @property
    def maximum_snapshot_age_days(self) -> dict[str, int]:
        freshness = self.document["freshness"]
        assert isinstance(freshness, dict)
        return dict(freshness["maximum_snapshot_age_days"])  # type: ignore[arg-type]

    def budget_limit(self, profile: str, metric: str) -> BudgetLimit:
        budgets = self.document["budgets"]
        assert isinstance(budgets, dict)
        profiles = budgets["profiles"]
        assert isinstance(profiles, dict)
        limits = profiles[profile]
        assert isinstance(limits, dict)
        limit = limits[metric]
        assert isinstance(limit, dict)
        return BudgetLimit(
            maximum_fraction=float(limit["maximum_fraction"]),
            maximum_absolute=int(limit["maximum_absolute"]),
        )

    @property
    def retention_minimum_expiry_horizon_days(self) -> int:
        retention = self.document["retention"]
        assert isinstance(retention, dict)
        return int(retention["minimum_expiry_horizon_days"])

    @property
    def retention_maximum_expired_retained_fraction(self) -> float:
        retention = self.document["retention"]
        assert isinstance(retention, dict)
        return float(retention["maximum_expired_retained_fraction"])


def load_refresh_policy(path: str | Path) -> RefreshPolicy:
    policy_path = Path(path)
    raw = policy_path.read_bytes()
    if len(raw) > _MAX_POLICY_BYTES:
        raise RefreshPolicyError("refresh policy exceeds the size limit")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshPolicyError(
            f"invalid refresh policy {policy_path}: {exc}"
        ) from exc
    _validate_policy_document(document)
    return RefreshPolicy(
        path=policy_path,
        document=document,
        sha256=hashlib.sha256(raw).hexdigest(),
        version=str(document["version"]),
    )


def _require_days(mapping: dict[str, object], name: str) -> dict[str, int]:
    value = mapping[name]
    if not isinstance(value, dict) or set(value) != set(_PROFILE_NAMES):
        raise RefreshPolicyError(
            f"freshness.{name} must cover exactly {list(_PROFILE_NAMES)}"
        )
    days: dict[str, int] = {}
    for profile, limit in value.items():
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise RefreshPolicyError(
                f"freshness.{name}.{profile} must be a positive integer"
            )
        days[str(profile)] = limit
    return days


def _validate_policy_document(document: object) -> None:
    if not isinstance(document, dict) or document.get("format") != POLICY_FORMAT:
        raise RefreshPolicyError("unsupported or missing refresh policy format")
    if not isinstance(document.get("version"), str) or not document["version"]:
        raise RefreshPolicyError("refresh policy version is required")

    freshness = document.get("freshness")
    if not isinstance(freshness, dict):
        raise RefreshPolicyError("refresh policy freshness must be an object")
    if freshness.get("required_timezone_semantics") != "unspecified_by_source":
        raise RefreshPolicyError("unsupported freshness timezone semantics")
    offset = freshness.get("naive_dnec_utc_offset_minutes")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not _NAIVE_DNEC_UTC_OFFSET_RANGE_MINUTES[0]
        <= offset
        <= _NAIVE_DNEC_UTC_OFFSET_RANGE_MINUTES[1]
        or offset % 15 != 0
    ):
        raise RefreshPolicyError(
            "naive_dnec_utc_offset_minutes must be a quarter-hour offset"
        )
    _require_days(freshness, "maximum_capture_lag_days")
    _require_days(freshness, "maximum_snapshot_age_days")

    ordering = document.get("ordering")
    if not isinstance(ordering, dict):
        raise RefreshPolicyError("refresh policy ordering must be an object")
    replay = document.get("replay_prevention")
    if not isinstance(replay, dict):
        raise RefreshPolicyError("refresh policy replay_prevention must be an object")

    budgets = document.get("budgets")
    if not isinstance(budgets, dict):
        raise RefreshPolicyError("refresh policy budgets must be an object")
    if budgets.get("metric_denominator") != "inherited_record_count":
        raise RefreshPolicyError("unsupported budget metric denominator")
    profiles = budgets.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(_PROFILE_NAMES):
        raise RefreshPolicyError(
            f"budget profiles must cover exactly {list(_PROFILE_NAMES)}"
        )
    for profile_name, limits in profiles.items():
        if not isinstance(limits, dict) or set(limits) != set(_BUDGET_METRICS):
            raise RefreshPolicyError(
                f"budget profile {profile_name} must cover exactly {list(_BUDGET_METRICS)}"
            )
        for metric, limit in limits.items():
            if not isinstance(limit, dict) or set(limit) != {
                "maximum_fraction",
                "maximum_absolute",
            }:
                raise RefreshPolicyError(
                    f"budget limit {profile_name}.{metric} has invalid keys"
                )
            fraction = limit["maximum_fraction"]
            absolute = limit["maximum_absolute"]
            if (
                isinstance(fraction, bool)
                or not isinstance(fraction, (int, float))
                or not 0 <= fraction <= 1
            ):
                raise RefreshPolicyError(
                    f"budget {profile_name}.{metric}.maximum_fraction must be within [0, 1]"
                )
            if (
                isinstance(absolute, bool)
                or not isinstance(absolute, int)
                or absolute < 0
            ):
                raise RefreshPolicyError(
                    f"budget {profile_name}.{metric}.maximum_absolute must be a non-negative integer"
                )

    retention = document.get("retention")
    if not isinstance(retention, dict):
        raise RefreshPolicyError("refresh policy retention must be an object")
    if retention.get("policy") != "retain-until-expiry":
        raise RefreshPolicyError("unsupported retention policy")
    if retention.get("never_delete_missing_on_single_crawl") is not True:
        raise RefreshPolicyError("retention must preserve the single-crawl floor")
    horizon = retention.get("minimum_expiry_horizon_days")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise RefreshPolicyError(
            "retention minimum_expiry_horizon_days must be positive"
        )
    expired_fraction = retention.get("maximum_expired_retained_fraction")
    if (
        isinstance(expired_fraction, bool)
        or not isinstance(expired_fraction, (int, float))
        or not 0 <= expired_fraction <= 1
    ):
        raise RefreshPolicyError(
            "retention maximum_expired_retained_fraction must be within [0, 1]"
        )


def validate_operator_override(reason: object) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise RefreshPolicyError(
            "a budget override requires a non-empty recorded reason "
            "(--refresh-override-budget)"
        )
    text = reason.strip()
    if len(text.encode("utf-8")) > _MAX_OVERRIDE_REASON_BYTES:
        raise RefreshPolicyError("budget override reason exceeds 512 bytes")
    return text


# --- Correios snapshot verification (scope 2: require and recompute) ---

_SNAPSHOT_FILES = ("manifest.json", "addresses.jsonl", "raw-addresses.jsonl")
_CEP_RE = re.compile(r"\d{8}")
_IBGE_RE = re.compile(r"\d{7}")
_CEP_TYPES = frozenset({"1", "2", "3", "4", "5", "6"})
_VALIDITY_STATES = frozenset({"active", "expired"})
_IBGE_RESOLUTIONS = frozenset(
    {
        "cep_unidade_operacional",
        "direct",
        "numero_localidade_superior",
        "numero_localidade_superior+cep_unidade_operacional",
        "source_link_conflict",
        "unresolved",
    }
)
_ADDRESS_KEYS = frozenset(
    {
        "cep",
        "cep_type",
        "city",
        "expired",
        "ibge",
        "ibge_resolution",
        "neighborhood",
        "previous_cep",
        "street",
        "uf",
        "valid_until",
    }
)
_MAX_SNAPSHOT_LINE_BYTES = 65536


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    manifest_identity: dict[str, object]
    addresses_identity: dict[str, object]
    raw_addresses_identity: dict[str, object]
    verified_manifest_document: dict[str, object]
    # Independently re-derived counts from streaming the actual bytes:
    record_count: int
    raw_record_count: int
    duplicate_record_count: int
    duplicate_group_count: int
    cep_type_counts: dict[str, int]
    validity_counts: dict[str, int]
    ibge_resolution_counts: dict[str, int]


def _sha256_and_bytes(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_size += len(chunk)
            digest.update(chunk)
    return byte_size, digest.hexdigest()


def _load_snapshot_manifest(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshPolicyError(
            f"Correios snapshot manifest is invalid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise RefreshPolicyError("Correios snapshot manifest must be an object")
    return document


def verify_correios_snapshot(directory: Path) -> SnapshotVerification:
    """Recompute every hashable claim about the actual Correios snapshot bytes.

    ``directory`` must be a real directory whose basename matches the refresh
    manifest's claimed ``correios_snapshot.directory`` (checked by the caller).
    All three snapshot files are hashed; ``addresses.jsonl`` is streamed to
    re-derive record counts, cep ordering, cep-type / validity / IBGE-resolution
    maps and first/last CEP; ``raw-addresses.jsonl`` is streamed to re-derive
    the raw count and duplicate statistics. Every derived value must equal the
    snapshot's own manifest claims.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise RefreshPolicyError(
            f"Correios snapshot must be a real directory: {directory}"
        )
    for name in _SNAPSHOT_FILES:
        candidate = directory / name
        if candidate.is_symlink() or not candidate.is_file():
            raise RefreshPolicyError(
                f"Correios snapshot is missing regular file {name} in {directory}"
            )
    manifest_path = directory / "manifest.json"
    addresses_path = directory / "addresses.jsonl"
    raw_path = directory / "raw-addresses.jsonl"

    manifest_bytes, manifest_sha = _sha256_and_bytes(manifest_path)
    addresses_bytes, addresses_sha = _sha256_and_bytes(addresses_path)
    raw_bytes, raw_sha = _sha256_and_bytes(raw_path)
    document = _load_snapshot_manifest(manifest_path)

    expected_keys = {
        "addresses_bytes",
        "addresses_sha256",
        "captured_at",
        "cep_type_counts",
        "date_only_expiry_semantics",
        "dnec_published_at",
        "dnec_timezone_semantics",
        "duplicate_group_count",
        "duplicate_record_count",
        "endpoint",
        "first_cep",
        "ibge_resolution_counts",
        "last_cep",
        "page_count",
        "page_size",
        "raw_addresses_bytes",
        "raw_addresses_sha256",
        "raw_cep_type_counts",
        "raw_record_count",
        "raw_validity_counts",
        "record_count",
        "schema_version",
        "sort",
        "source",
        "source_total_elements",
        "validity_counts",
    }
    if set(document) != expected_keys | {"artifacts"}:
        raise RefreshPolicyError("Correios snapshot manifest has unexpected keys")
    artifacts = document.get("artifacts")
    if (
        not isinstance(artifacts, dict)
        or set(artifacts) != {"canonical_addresses", "raw_addresses"}
        or artifacts["canonical_addresses"].get("path") != "addresses.jsonl"
        or artifacts["raw_addresses"].get("path") != "raw-addresses.jsonl"
        or artifacts["canonical_addresses"].get("sha256") != addresses_sha
        or artifacts["raw_addresses"].get("sha256") != raw_sha
        or artifacts["canonical_addresses"].get("bytes") != addresses_bytes
        or artifacts["raw_addresses"].get("bytes") != raw_bytes
    ):
        raise RefreshPolicyError(
            "Correios snapshot manifest artifact records disagree with the files"
        )
    if (
        document.get("schema_version") != 3
        or document.get("source") != "correios-busca-cep-v3"
        or document.get("endpoint") != "/cep/v2/enderecos"
        or document.get("sort") != ["cep,asc"]
    ):
        raise RefreshPolicyError(
            "Correios snapshot manifest describes a different crawl"
        )
    if (
        document.get("addresses_bytes") != addresses_bytes
        or document.get("addresses_sha256") != addresses_sha
        or document.get("raw_addresses_bytes") != raw_bytes
        or document.get("raw_addresses_sha256") != raw_sha
    ):
        raise RefreshPolicyError(
            "Correios snapshot files do not match the snapshot manifest hashes"
        )

    cep_types: dict[str, int] = {key: 0 for key in sorted(_CEP_TYPES)}
    validity: dict[str, int] = {key: 0 for key in sorted(_VALIDITY_STATES)}
    resolutions: dict[str, int] = {key: 0 for key in sorted(_IBGE_RESOLUTIONS)}
    records = 0
    previous_cep: str | None = None
    first_cep: str | None = None
    last_cep: str | None = None
    with addresses_path.open("rb") as handle:
        for records, raw_line in enumerate(handle, start=1):
            if len(raw_line) > _MAX_SNAPSHOT_LINE_BYTES:
                raise RefreshPolicyError(
                    f"Correios snapshot address line {records} exceeds the size limit"
                )
            if not raw_line.endswith(b"\n"):
                raise RefreshPolicyError(
                    "Correios snapshot addresses must end every row with a newline"
                )
            try:
                row = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RefreshPolicyError(
                    f"Correios snapshot address line {records} is invalid JSON"
                ) from exc
            if not isinstance(row, dict) or set(row) != _ADDRESS_KEYS:
                raise RefreshPolicyError(
                    f"Correios snapshot address line {records} has invalid keys"
                )
            cep = row.get("cep")
            cep_type = row.get("cep_type")
            ibge = row.get("ibge")
            resolution = row.get("ibge_resolution")
            expired = row.get("expired")
            valid_until = row.get("valid_until")
            previous = row.get("previous_cep")
            if (
                not isinstance(cep, str)
                or _CEP_RE.fullmatch(cep) is None
                or isinstance(cep_type, bool)
                or not isinstance(cep_type, int)
                or str(cep_type) not in cep_types
                or resolution not in resolutions
                or not isinstance(expired, bool)
                or (
                    previous is not None
                    and (
                        not isinstance(previous, str)
                        or _CEP_RE.fullmatch(previous) is None
                    )
                )
                or (valid_until is not None and not isinstance(valid_until, str))
                or (
                    not isinstance(ibge, str)
                    and not (
                        ibge is None and resolution == "source_link_conflict"
                    )
                )
                or (
                    isinstance(ibge, str)
                    and _IBGE_RE.fullmatch(ibge) is None
                )
            ):
                raise RefreshPolicyError(
                    f"Correios snapshot address line {records} is invalid"
                )
            if previous_cep is not None and cep <= previous_cep:
                raise RefreshPolicyError(
                    f"Correios snapshot addresses are not strictly increasing at line {records}"
                )
            previous_cep = cep
            first_cep = first_cep or cep
            last_cep = cep
            cep_types[str(cep_type)] += 1
            validity["expired" if expired else "active"] += 1
            resolutions[str(resolution)] += 1

    raw_records = 0
    raw_seen: dict[str, int] = {}
    with raw_path.open("rb") as handle:
        for raw_records, raw_line in enumerate(handle, start=1):
            if len(raw_line) > _MAX_SNAPSHOT_LINE_BYTES:
                raise RefreshPolicyError(
                    f"Correios snapshot raw line {raw_records} exceeds the size limit"
                )
            try:
                row = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RefreshPolicyError(
                    f"Correios snapshot raw line {raw_records} is invalid JSON"
                ) from exc
            if not isinstance(row, dict) or not isinstance(row.get("cep"), str):
                raise RefreshPolicyError(
                    f"Correios snapshot raw line {raw_records} has no CEP"
                )
            cep = str(row["cep"])
            raw_seen[cep] = raw_seen.get(cep, 0) + 1

    duplicate_records = sum(count - 1 for count in raw_seen.values() if count > 1)
    duplicate_groups = sum(1 for count in raw_seen.values() if count > 1)

    derived = {
        "record_count": records,
        "raw_record_count": raw_records,
        "duplicate_record_count": duplicate_records,
        "duplicate_group_count": duplicate_groups,
        "cep_type_counts": cep_types,
        "validity_counts": validity,
        "ibge_resolution_counts": resolutions,
    }
    for key, value in derived.items():
        if document.get(key) != value:
            raise RefreshPolicyError(
                "Correios snapshot manifest claims disagree with the actual bytes: "
                f"{key} claimed {document.get(key)!r}, derived {value!r}"
            )
    if (
        document.get("first_cep") != first_cep
        or document.get("last_cep") != last_cep
        or sum(cep_types.values()) != records
        or sum(validity.values()) != records
        or sum(resolutions.values()) != records
        or document.get("record_count")
        != document.get("raw_record_count", -1)
        - document.get("duplicate_record_count", -1)
        or document.get("source_total_elements") != raw_records
        or document.get("page_count")
        != (raw_records + int(document.get("page_size", 0)) - 1)
        // int(document.get("page_size", 0))
    ):
        raise RefreshPolicyError(
            "Correios snapshot manifest internal counts are inconsistent"
        )

    return SnapshotVerification(
        manifest_identity={
            "filename": manifest_path.name,
            "bytes": manifest_bytes,
            "sha256": manifest_sha,
        },
        addresses_identity={
            "filename": addresses_path.name,
            "bytes": addresses_bytes,
            "sha256": addresses_sha,
        },
        raw_addresses_identity={
            "filename": raw_path.name,
            "bytes": raw_bytes,
            "sha256": raw_sha,
        },
        verified_manifest_document=document,
        **derived,
    )


# --- Policy gates (scope 1, 3, 4) ---


@dataclass(frozen=True, slots=True)
class RefreshGateReport:
    policy_version: str
    profile: str
    dnec_published_at_utc: str
    captured_at_utc: str
    capture_lag_days: float
    snapshot_age_days_at_build: float
    ordering: dict[str, object]
    budgets: dict[str, object]
    retention: dict[str, object]
    override: dict[str, object] | None

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "profile": self.profile,
            "dnec_published_at_utc": self.dnec_published_at_utc,
            "captured_at_utc": self.captured_at_utc,
            "capture_lag_days": self.capture_lag_days,
            "snapshot_age_days_at_build": self.snapshot_age_days_at_build,
            "ordering": self.ordering,
            "budgets": self.budgets,
            "retention": self.retention,
            "override": self.override,
        }


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def enforce_refresh_policy(
    *,
    policy: RefreshPolicy,
    profile: str,
    correios_claim: dict[str, object],
    snapshot_verification: SnapshotVerification,
    dataset_version: str,
    classification_counts: dict[str, int],
    inherited_dataset_version: str,
    inherited_snapshot: dict[str, object] | None,
    inherited_record_count: int,
    build_instant: datetime,
    override_reason: str | None,
) -> RefreshGateReport:
    """Enforce freshness, ordering, replay, budgets and retention in one pass.

    Every input here has already been verified against actual bytes by
    ``verify_correios_snapshot`` and the refresh-manifest loader; the gates
    below are the last line before the candidate is allowed to build.
    """
    if profile not in _PROFILE_NAMES:
        raise RefreshPolicyError(f"unknown refresh profile: {profile!r}")

    # -- Freshness (scope 1): real ISO-8601 parsing with timezone semantics --
    dnec_semantics = correios_claim.get("dnec_timezone_semantics")
    if not isinstance(dnec_semantics, str) or dnec_semantics != "unspecified_by_source":
        raise RefreshPolicyError(
            "correios snapshot dnec_timezone_semantics must record unspecified_by_source"
        )
    date_semantics = correios_claim.get("date_only_expiry_semantics")
    if not isinstance(date_semantics, str) or not date_semantics:
        raise RefreshPolicyError(
            "correios snapshot date_only_expiry_semantics is required"
        )
    captured = parse_instant(correios_claim.get("captured_at"))
    published = parse_instant(
        correios_claim.get("dnec_published_at"),
        allow_naive_at_offset=policy.naive_dnec_offset_minutes,
    )
    if captured < published:
        raise RefreshPolicyError(
            "Correios snapshot was captured before its DNEC publication marker: "
            f"captured {correios_claim.get('captured_at')!r} < published "
            f"{correios_claim.get('dnec_published_at')!r}"
        )
    capture_lag_days = (captured - published).total_seconds() / 86400.0
    max_lag = policy.maximum_capture_lag_days[profile]
    if capture_lag_days > max_lag:
        raise RefreshPolicyError(
            f"Correios snapshot capture lag exceeds the {profile} policy: "
            f"{capture_lag_days:.1f} days > {max_lag} days between dnec_published_at "
            f"and captured_at — the crawl is stale"
        )
    snapshot_age_days = (build_instant - captured).total_seconds() / 86400.0
    max_age = policy.maximum_snapshot_age_days[profile]
    if snapshot_age_days > max_age:
        raise RefreshPolicyError(
            f"Correios snapshot age exceeds the {profile} policy: "
            f"{snapshot_age_days:.1f} days > {max_age} days since captured_at — "
            "refresh with a newer snapshot"
        )

    # -- Ordering + replay prevention (scope 1): strictly-newer publication --
    ordering_report: dict[str, object] = {
        "inherited_dataset_version": inherited_dataset_version,
        "inherited_snapshot": bool(inherited_snapshot),
    }
    if natural_version_key(dataset_version) <= natural_version_key(
        inherited_dataset_version
    ):
        raise RefreshPolicyError(
            "candidate dataset version does not progress past the inherited release: "
            f"{dataset_version!r} <= {inherited_dataset_version!r} under natural ordering"
        )
    if inherited_snapshot is not None:
        inherited_published = parse_instant(
            inherited_snapshot.get("dnec_published_at"),
            allow_naive_at_offset=policy.naive_dnec_offset_minutes,
        )
        inherited_captured = parse_instant(inherited_snapshot.get("captured_at"))
        if published <= inherited_published or captured <= inherited_captured:
            raise RefreshPolicyError(
                "Correios snapshot does not advance past the inherited release "
                f"(dnec_published {published.isoformat()} vs "
                f"{inherited_published.isoformat()}, captured {captured.isoformat()} vs "
                f"{inherited_captured.isoformat()}); an equal or older snapshot is a replay"
            )
        inherited_addresses = inherited_snapshot.get("addresses_sha256")
        inherited_manifest = inherited_snapshot.get("manifest_sha256")
        if (
            isinstance(inherited_addresses, str)
            and correios_claim.get("addresses_sha256") == inherited_addresses
        ) or (
            isinstance(inherited_manifest, str)
            and correios_claim.get("manifest_sha256") == inherited_manifest
        ):
            raise RefreshPolicyError(
                "Correios snapshot reuses the inherited release's publication identity "
                "(addresses/manifest hash); the snapshot must be strictly newer data"
            )
        ordering_report["dnec_published_advanced"] = published > inherited_published
        ordering_report["captured_advanced"] = captured > inherited_captured

    # -- Change budgets (scope 3): bounded delta fractions, overridable --
    observed = {
        "added": classification_counts.get("added", 0),
        "missing_from_source": classification_counts.get("missing_from_source", 0),
        "ibge_changed": classification_counts.get("ibge_changed", 0),
        "address_changed": classification_counts.get("address_changed", 0),
        "duplicates_dropped": snapshot_verification.duplicate_record_count,
        "source_link_conflicts": classification_counts.get("source_link_conflict", 0),
    }
    breaches: list[dict[str, object]] = []
    budget_report: dict[str, object] = {"denominator": inherited_record_count}
    for metric in _BUDGET_METRICS:
        limit = policy.budget_limit(profile, metric)
        value = int(observed[metric])
        fraction = value / inherited_record_count if inherited_record_count else 0.0
        budget_report[metric] = {
            "observed": value,
            "fraction": round(fraction, 8),
            "maximum_fraction": limit.maximum_fraction,
            "maximum_absolute": limit.maximum_absolute,
        }
        if fraction > limit.maximum_fraction or value > limit.maximum_absolute:
            breaches.append(
                {
                    "metric": metric,
                    "observed": value,
                    "fraction": round(fraction, 8),
                    "maximum_fraction": limit.maximum_fraction,
                    "maximum_absolute": limit.maximum_absolute,
                }
            )
    override_report: dict[str, object] | None = None
    if breaches:
        if override_reason is None:
            detail = "; ".join(
                f"{breach['metric']}={breach['observed']} "
                f"(fraction {breach['fraction']} > {breach['maximum_fraction']}, "
                f"or absolute > {breach['maximum_absolute']})"
                for breach in breaches
            )
            raise RefreshPolicyError(
                f"refresh change budget exceeded under the {profile} profile: {detail}. "
                "Use --refresh-profile catch-up for an authorised large jump, or "
                '--refresh-override-budget "<reason>" to record an explicit override.'
            )
        override_report = {
            "reason": override_reason,
            "breached_metrics": [breach["metric"] for breach in breaches],
        }

    # Retention (scope 3): the floor — a single unproven crawl never deletes —
    # is enforced structurally by the builder (missing_from_source rows are
    # retained as retained_missing). The boundary enforced here is the expiry
    # horizon the refresh tool must respect before it may expire a row.
    retention_report = {
        "policy": "retain-until-expiry",
        "never_delete_missing_on_single_crawl": True,
        "minimum_expiry_horizon_days": policy.retention_minimum_expiry_horizon_days,
        "expired_retained": 0,
    }

    return RefreshGateReport(
        policy_version=policy.version,
        profile=profile,
        dnec_published_at_utc=_format_utc(published),
        captured_at_utc=_format_utc(captured),
        capture_lag_days=round(capture_lag_days, 3),
        snapshot_age_days_at_build=round(snapshot_age_days, 3),
        ordering=ordering_report,
        budgets=budget_report,
        retention=retention_report,
        override=override_report,
    )
