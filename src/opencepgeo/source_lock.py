from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SourceLockError(ValueError):
    """The source lock or a locked input failed validation."""


@dataclass(frozen=True)
class RefreshPolicy:
    refresh_interval_days: int
    max_age_days: int

    def as_dict(self) -> dict[str, int]:
        return {
            "refresh_interval_days": self.refresh_interval_days,
            "max_age_days": self.max_age_days,
        }


@dataclass(frozen=True)
class LockedSource:
    source_id: str
    role: str
    required: bool
    version: str
    filename: str
    byte_size: int
    sha256: str
    acquisition: str
    url: str | None
    local_path: str | None
    refresh_policy: RefreshPolicy | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class SourceLock:
    path: Path
    release: str
    sources: tuple[LockedSource, ...]


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLockError(f"{field} must be a non-empty string")
    return value


def _validate_refresh_policy(value: object, field: str) -> RefreshPolicy | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "refresh_interval_days",
        "max_age_days",
    }:
        raise SourceLockError(
            f"{field} must contain exactly refresh_interval_days and max_age_days"
        )
    days: list[int] = []
    for name in ("refresh_interval_days", "max_age_days"):
        raw = value[name]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1 or raw > 36500:
            raise SourceLockError(
                f"{field}.{name} must be an integer between 1 and 36500 days"
            )
        days.append(raw)
    refresh_interval_days, max_age_days = days
    if max_age_days < refresh_interval_days:
        raise SourceLockError(
            f"{field}.max_age_days must not be shorter than refresh_interval_days"
        )
    return RefreshPolicy(
        refresh_interval_days=refresh_interval_days,
        max_age_days=max_age_days,
    )


def parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SourceLockError(f"{field} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceLockError(f"{field} is not a valid timestamp: {exc}") from exc
    if moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(None):
        raise SourceLockError(f"{field} must be expressed in UTC")
    return moment


def source_age_status(
    source: LockedSource, now: datetime | None = None
) -> dict[str, object]:
    """Classify a locked source against its refresh policy.

    Sources without a refresh_policy (repository-owned fixtures and the like)
    never age. ``now`` defaults to the current UTC time.
    """
    moment = now or datetime.now(timezone.utc)
    retrieved_at = parse_timestamp(
        source.metadata.get("retrieved_at"), f"{source.source_id}.retrieved_at"
    )
    age_days = (moment - retrieved_at).total_seconds() / 86400.0
    if age_days < 0:
        raise SourceLockError(
            f"{source.source_id}.retrieved_at is in the future relative to the check"
        )
    if source.refresh_policy is None:
        return {
            "id": source.source_id,
            "status": "no-policy",
            "age_days": round(age_days, 2),
            "refresh_interval_days": None,
            "max_age_days": None,
            "next_refresh_days": None,
        }
    return {
        "id": source.source_id,
        "status": (
            "stale"
            if age_days > source.refresh_policy.max_age_days
            else (
                "due"
                if age_days > source.refresh_policy.refresh_interval_days
                else "current"
            )
        ),
        "age_days": round(age_days, 2),
        "refresh_interval_days": source.refresh_policy.refresh_interval_days,
        "max_age_days": source.refresh_policy.max_age_days,
        "next_refresh_days": round(
            source.refresh_policy.refresh_interval_days - age_days, 2
        ),
    }


def _validate_member_identities(value: object, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not value:
        raise SourceLockError(f"{field} must be a non-empty object")
    casefolded_names: set[str] = set()
    for name, identity in value.items():
        member_field = f"{field}.{name}"
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or "\\" in name
            or name.casefold() in casefolded_names
        ):
            raise SourceLockError(
                f"{field} contains an unsafe or duplicate member name"
            )
        casefolded_names.add(name.casefold())
        if not isinstance(identity, dict) or set(identity) != {"bytes", "sha256"}:
            raise SourceLockError(
                f"{member_field} must contain exactly bytes and sha256"
            )
        byte_size = identity.get("bytes")
        if (
            not isinstance(byte_size, int)
            or isinstance(byte_size, bool)
            or byte_size < 1
        ):
            raise SourceLockError(f"{member_field}.bytes must be a positive integer")
        sha256 = identity.get("sha256")
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise SourceLockError(
                f"{member_field}.sha256 must be lowercase hexadecimal"
            )


def load_source_lock(path: str | Path) -> SourceLock:
    lock_path = Path(path).resolve()
    try:
        document = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceLockError(f"cannot read source lock {lock_path}: {exc}") from exc

    if (
        not isinstance(document, dict)
        or document.get("format") != "opencepgeo-source-lock-v1"
    ):
        raise SourceLockError("unsupported or missing source lock format")
    release = _require_string(document.get("release"), "release")
    _require_string(document.get("publication_gate"), "publication_gate")
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SourceLockError("sources must be a non-empty list")

    sources: list[LockedSource] = []
    source_ids: set[str] = set()
    filenames: set[str] = set()
    for index, raw in enumerate(raw_sources):
        prefix = f"sources[{index}]"
        if not isinstance(raw, dict):
            raise SourceLockError(f"{prefix} must be an object")
        source_id = _require_string(raw.get("id"), f"{prefix}.id")
        filename = _require_string(raw.get("filename"), f"{prefix}.filename")
        if Path(filename).name != filename:
            raise SourceLockError(f"{prefix}.filename must be a basename")
        if source_id in source_ids:
            raise SourceLockError(f"duplicate source id: {source_id}")
        if filename in filenames:
            raise SourceLockError(f"duplicate source filename: {filename}")

        required = raw.get("required")
        if not isinstance(required, bool):
            raise SourceLockError(f"{prefix}.required must be a boolean")
        byte_size = raw.get("bytes")
        if not isinstance(byte_size, int) or byte_size < 0:
            raise SourceLockError(f"{prefix}.bytes must be a non-negative integer")
        sha256 = _require_string(raw.get("sha256"), f"{prefix}.sha256")
        if len(sha256) != 64 or any(
            character not in "0123456789abcdef" for character in sha256
        ):
            raise SourceLockError(f"{prefix}.sha256 must be lowercase hexadecimal")

        acquisition = _require_string(raw.get("acquisition"), f"{prefix}.acquisition")
        for metadata_field in (
            "retrieved_at",
            "attribution",
            "license_status",
            "terms_status",
        ):
            _require_string(raw.get(metadata_field), f"{prefix}.{metadata_field}")
        parse_timestamp(raw.get("retrieved_at"), f"{prefix}.retrieved_at")
        refresh_policy = _validate_refresh_policy(
            raw.get("refresh_policy"), f"{prefix}.refresh_policy"
        )
        _validate_member_identities(raw.get("members"), f"{prefix}.members")
        url = raw.get("url")
        local_path = raw.get("local_path")
        if acquisition == "https":
            url = _require_string(url, f"{prefix}.url")
            if urllib.parse.urlparse(url).scheme != "https":
                raise SourceLockError(f"{prefix}.url must use HTTPS")
            if local_path is not None:
                raise SourceLockError(
                    f"{prefix}.local_path is invalid for HTTPS acquisition"
                )
        elif acquisition == "repository":
            local_path = _require_string(local_path, f"{prefix}.local_path")
            if Path(local_path).is_absolute():
                raise SourceLockError(f"{prefix}.local_path must be relative")
            if url is not None:
                raise SourceLockError(
                    f"{prefix}.url is invalid for repository acquisition"
                )
        else:
            raise SourceLockError(
                f"{prefix}.acquisition must be 'https' or 'repository'"
            )

        sources.append(
            LockedSource(
                source_id=source_id,
                role=_require_string(raw.get("role"), f"{prefix}.role"),
                required=required,
                version=_require_string(raw.get("version"), f"{prefix}.version"),
                filename=filename,
                byte_size=byte_size,
                sha256=sha256,
                acquisition=acquisition,
                url=url if isinstance(url, str) else None,
                local_path=local_path if isinstance(local_path, str) else None,
                refresh_policy=refresh_policy,
                metadata=raw,
            )
        )
        source_ids.add(source_id)
        filenames.add(filename)
    return SourceLock(path=lock_path, release=release, sources=tuple(sources))


def _select_sources(
    lock: SourceLock,
    source_ids: list[str] | tuple[str, ...] | None,
    include_optional: bool,
) -> tuple[LockedSource, ...]:
    if source_ids:
        requested = set(source_ids)
        available = {source.source_id for source in lock.sources}
        unknown = sorted(requested - available)
        if unknown:
            raise SourceLockError(f"unknown source id(s): {', '.join(unknown)}")
        return tuple(source for source in lock.sources if source.source_id in requested)
    return tuple(
        source for source in lock.sources if source.required or include_optional
    )


def verify_file(path: str | Path, source: LockedSource) -> dict[str, object]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise SourceLockError(f"locked input must not be a symlink: {candidate}")
    if not candidate.is_file():
        raise SourceLockError(f"locked input is missing: {candidate}")

    digest = hashlib.sha256()
    byte_size = 0
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_size += len(chunk)
            digest.update(chunk)
    if byte_size != source.byte_size:
        raise SourceLockError(
            f"size mismatch for {source.source_id}: expected {source.byte_size}, got {byte_size}"
        )
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != source.sha256:
        raise SourceLockError(
            f"SHA-256 mismatch for {source.source_id}: expected {source.sha256}, "
            f"got {actual_sha256}"
        )
    return {
        "id": source.source_id,
        "path": str(candidate.resolve()),
        "bytes": byte_size,
        "sha256": actual_sha256,
    }


def verify_sources(
    lock_path: str | Path,
    input_directory: str | Path,
    *,
    source_ids: list[str] | tuple[str, ...] | None = None,
    include_optional: bool = False,
) -> list[dict[str, object]]:
    lock = load_source_lock(lock_path)
    selected = _select_sources(lock, source_ids, include_optional)
    input_root = Path(input_directory)
    return [verify_file(input_root / source.filename, source) for source in selected]


def repository_source_path(lock: SourceLock, source: LockedSource) -> Path:
    if source.acquisition != "repository":
        raise SourceLockError(f"source is not repository-acquired: {source.source_id}")
    repository_root = lock.path.parent.parent.resolve()
    candidate = (repository_root / (source.local_path or "")).resolve()
    if candidate != repository_root and repository_root not in candidate.parents:
        raise SourceLockError(
            f"repository source escapes repository root: {source.source_id}"
        )
    return candidate


def _copy_repository_source(lock: SourceLock, source: LockedSource, output) -> None:
    candidate = repository_source_path(lock, source)
    verify_file(candidate, source)
    with candidate.open("rb") as handle:
        shutil.copyfileobj(handle, output, length=1024 * 1024)


def _download_source(source: LockedSource, output, timeout: float) -> None:
    request = urllib.request.Request(
        source.url or "",
        headers={"User-Agent": "OpenCEPGeo source-lock-v1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    advertised_size = int(content_length)
                except ValueError as exc:
                    raise SourceLockError(
                        f"invalid Content-Length for {source.source_id}"
                    ) from exc
                if advertised_size != source.byte_size:
                    raise SourceLockError(
                        f"download size mismatch for {source.source_id}: "
                        f"expected {source.byte_size}, server advertised {advertised_size}"
                    )
            remaining = source.byte_size
            while remaining:
                chunk = response.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise SourceLockError(
                        f"download ended early for {source.source_id}: "
                        f"expected {source.byte_size} bytes"
                    )
                output.write(chunk)
                remaining -= len(chunk)
            if response.read(1):
                raise SourceLockError(
                    f"download exceeds locked size for {source.source_id}: "
                    f"expected {source.byte_size} bytes"
                )
    except SourceLockError:
        raise
    except OSError as exc:
        raise SourceLockError(f"download failed for {source.source_id}: {exc}") from exc


def fetch_sources(
    lock_path: str | Path,
    input_directory: str | Path,
    *,
    source_ids: list[str] | tuple[str, ...] | None = None,
    include_optional: bool = False,
    timeout: float = 60.0,
) -> list[dict[str, object]]:
    lock = load_source_lock(lock_path)
    selected = _select_sources(lock, source_ids, include_optional)
    input_root = Path(input_directory)
    input_root.mkdir(parents=True, exist_ok=True)
    if input_root.is_symlink() or not input_root.is_dir():
        raise SourceLockError(f"input directory is not a real directory: {input_root}")

    results: list[dict[str, object]] = []
    for source in selected:
        destination = input_root / source.filename
        if destination.exists() or destination.is_symlink():
            results.append(verify_file(destination, source))
            continue

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{source.filename}.",
                suffix=".part",
                dir=input_root,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                if source.acquisition == "repository":
                    _copy_repository_source(lock, source, temporary)
                else:
                    _download_source(source, temporary, timeout)
                temporary.flush()
                os.fsync(temporary.fileno())
            result = verify_file(temporary_path, source)
            os.replace(temporary_path, destination)
            temporary_path = None
            result["path"] = str(destination.resolve())
            results.append(result)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    return results
