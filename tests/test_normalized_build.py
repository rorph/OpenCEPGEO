import hashlib
import io
import json
import shutil
import sqlite3
import stat
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from opencepgeo import database as database_module
from opencepgeo.cli import main as cli_main
from opencepgeo.database import (
    _load_refresh_manifest,
    build_database_from_normalized,
    lookup,
)
from opencepgeo.quality import (
    build_quality_report,
    quality_report_markdown,
    write_quality_report,
)
from opencepgeo.release import _attribution_tokens, package_release, verify_release
from tests.helpers import write_municipality_boundaries
from tests.test_pipeline import make_ibge_gpkg
from tests.test_release import _write_quality_policy, _write_validation_inputs


DATASET_VERSION = "fixture-dnc-v2"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _canonical_row(row: dict[str, object]) -> bytes:
    return (
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _rows() -> list[dict[str, object]]:
    return [
        {
            "cep": "01001000",
            "prefix": "01001",
            "street": "Praça da Sé",
            "complement": None,
            "unit": None,
            "neighborhood": "Sé",
            "city": "São Paulo",
            "uf": "SP",
            "state": "São Paulo",
            "region": "Sudeste",
            "ibge": "3550308",
            "dataset_version": DATASET_VERSION,
            "geo": {
                "type": "Point",
                "coordinates": [-46.6333, -23.5505],
                "precision": "municipality",
                "method": "ibge_municipality_reference",
                "evidence_count": 1,
                "evidence_radius_km": 0.0,
                "source": ["ibge"],
                "evidence_digest": "sha256:" + "0" * 64,
            },
        },
        {
            "cep": "20010000",
            "prefix": "20010",
            "street": None,
            "complement": None,
            "unit": None,
            "neighborhood": None,
            "city": "Rio de Janeiro",
            "uf": "RJ",
            "state": "Rio de Janeiro",
            "region": "Sudeste",
            "ibge": "3304557",
            "dataset_version": DATASET_VERSION,
            "geo": None,
        },
        {
            "cep": "53990959",
            "prefix": "53990",
            "street": None,
            "complement": None,
            "unit": None,
            "neighborhood": None,
            "city": "Fernando de Noronha",
            "uf": "PE",
            "state": "Pernambuco",
            "region": "Nordeste",
            "ibge": "2605459",
            "dataset_version": DATASET_VERSION,
            "geo": None,
        },
    ]


def _fixture_identity(filename: str, file_format: str, fill: str) -> dict[str, object]:
    return {
        "filename": filename,
        "format": file_format,
        "bytes": 1,
        "sha256": fill * 64,
    }


def _inherited_release() -> dict[str, object]:
    return {
        "build_manifest": _fixture_identity(
            "build-manifest.json", "opencepgeo-build-manifest-v2", "a"
        ),
        "builder": {
            "name": "opencepgeo",
            "version": "0.2.0rc2",
            "source_tree_sha256": "b" * 64,
        },
        "contract": _fixture_identity(
            "opencepgeo-v4-fixture-current-v1.json",
            "px-opencepgeo-import-contract-v1",
            "c",
        ),
        "dataset_version": "fixture-current-v1",
        "enrichment": _fixture_identity(
            "enrichment-config.json", "opencepgeo-enrichment-v1", "d"
        ),
        "normalized_artifact": {
            "filename": "current.jsonl",
            "format": "opencepgeo-jsonl-v4",
            "bytes": 1,
            "sha256": "1" * 64,
        },
        "publication_gate": "blocked-fixture-rights-review",
        "quality_pass_value": "quality-fixture-current-v1",
        "quality_policy": _fixture_identity(
            "quality-policy.json", "opencepgeo-quality-policy-v2", "e"
        ),
        "quality_report": _fixture_identity(
            "quality-report.json", "opencepgeo-quality-report-v2", "f"
        ),
        "record_count": 1,
        "release_manifest": _fixture_identity(
            "manifest.json", "opencepgeo-release-manifest-v2", "0"
        ),
        "release_status": "blocked-private-release-candidate",
        "source_lock": _fixture_identity(
            "source-lock.json", "opencepgeo-source-lock-v1", "9"
        ),
    }


def _correios_snapshot(directory: Path, row_count: int = 3) -> dict[str, object]:
    """Refresh-manifest Correios claims, fully bound to real snapshot bytes.

    The refresh manifest carries the crawl's scalar claims plus the snapshot
    manifest's own hash; the crawl manifest's per-file ``artifacts`` records
    are verified against the bytes by the snapshot verifier and are not part
    of the refresh-manifest claim set.
    """
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        key: value
        for key, value in manifest.items()
        if key != "artifacts"
    } | {
        "directory": directory.name,
        "manifest_sha256": _sha256(manifest_path),
    }


def _classification_counts(row_count: int) -> dict[str, int]:
    return {
        "added": row_count - 1,
        "address_changed": 0,
        "ibge_changed": 0,
        "missing_from_source": 0,
        "source_link_conflict": 0,
        "unchanged": 1,
    }


def _geography_action_counts(row_count: int) -> dict[str, int]:
    return {
        "assigned_municipality": 0,
        "invalidated_exact_to_municipality": 0,
        "preserved": 1,
        "reassigned_municipality": 0,
        "retained_missing": 0,
        "retained_source_link_conflict": 0,
        "unresolved": row_count - 1,
    }


def _write_diff(path: Path, rows: list[dict[str, object]]) -> None:
    records = []
    for index, row in enumerate(rows):
        records.append(
            {
                "candidate_included": True,
                "cep": row["cep"],
                "changed_fields": [],
                "classification": "unchanged" if index == 0 else "added",
                "current_ibge": None,
                "geography_action": "preserved" if index == 0 else "unresolved",
                "previous_cep": None,
                "previous_ibge": None,
                "valid_until": None,
            }
        )
    path.write_bytes(b"".join(_canonical_row(record) for record in records))


def _write_correios_snapshot(
    directory: Path,
    row_count: int,
    *,
    captured_at: str = "2026-08-11T01:30:00Z",
    dnec_published_at: str = "2026-08-11T00:00:00Z",
    tamper_manifest: bool = False,
) -> dict[str, object]:
    """Write a real crawl-snapshot directory and return its verified claims.

    The addresses/raw files are generated from the fixture rows so every count
    the refresh manifest claims is reproducible from actual bytes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    rows = _rows()[:row_count] if row_count <= 3 else _rows()
    addresses = [
        {
            "cep": row["cep"],
            "cep_type": 2,
            "city": row["city"],
            "expired": False,
            "ibge": row["ibge"],
            "ibge_resolution": "direct",
            "neighborhood": row["neighborhood"],
            "previous_cep": None,
            "street": row["street"],
            "uf": row["uf"],
            "valid_until": None,
        }
        for row in rows
    ]
    addresses_payload = b"".join(_canonical_row(record) for record in addresses)
    (directory / "addresses.jsonl").write_bytes(addresses_payload)
    (directory / "raw-addresses.jsonl").write_bytes(addresses_payload)
    addresses_sha = hashlib.sha256(addresses_payload).hexdigest()
    manifest = {
        "addresses_bytes": len(addresses_payload),
        "addresses_sha256": addresses_sha,
        "artifacts": {
            "canonical_addresses": {
                "bytes": len(addresses_payload),
                "format": "correios-cep-canonical-v3",
                "path": "addresses.jsonl",
                "records": len(addresses),
                "sha256": addresses_sha,
            },
            "raw_addresses": {
                "bytes": len(addresses_payload),
                "format": "correios-busca-cep-v3-normalized-raw-v1",
                "path": "raw-addresses.jsonl",
                "records": len(addresses),
                "sha256": addresses_sha,
            },
        },
        "captured_at": captured_at,
        "cep_type_counts": {
            "1": 0,
            "2": len(addresses),
            "3": 0,
            "4": 0,
            "5": 0,
            "6": 0,
        },
        "date_only_expiry_semantics": "active_through_date_using_utc_capture_date",
        "dnec_published_at": dnec_published_at,
        "dnec_timezone_semantics": "unspecified_by_source",
        "duplicate_group_count": 0,
        "duplicate_record_count": 0,
        "endpoint": "/cep/v2/enderecos",
        "first_cep": addresses[0]["cep"],
        "ibge_resolution_counts": {
            "cep_unidade_operacional": 0,
            "direct": len(addresses),
            "numero_localidade_superior": 0,
            "numero_localidade_superior+cep_unidade_operacional": 0,
            "source_link_conflict": 0,
            "unresolved": 0,
        },
        "last_cep": addresses[-1]["cep"],
        "page_count": 1,
        "page_size": 2000,
        "raw_addresses_bytes": len(addresses_payload),
        "raw_addresses_sha256": hashlib.sha256(addresses_payload).hexdigest(),
        "raw_cep_type_counts": {
            "1": 0,
            "2": len(addresses),
            "3": 0,
            "4": 0,
            "5": 0,
            "6": 0,
        },
        "raw_record_count": len(addresses),
        "raw_validity_counts": {"active": len(addresses), "expired": 0},
        "record_count": len(addresses),
        "schema_version": 3,
        "sort": ["cep,asc"],
        "source": "correios-busca-cep-v3",
        "source_total_elements": len(addresses),
        "validity_counts": {"active": len(addresses), "expired": 0},
    }
    if tamper_manifest:
        manifest["record_count"] = manifest["record_count"] + 1
    _write_json(directory / "manifest.json", manifest)
    return manifest


def _refresh_document(
    normalized: Path,
    quality: Path,
    diff: Path,
    dataset_version: str,
    row_count: int,
    *,
    correios_directory: Path | None = None,
    inherited: dict[str, object] | None = None,
    current: dict[str, object] | None = None,
    contract: dict[str, object] | None = None,
) -> dict[str, object]:
    inherited = inherited or _inherited_release()
    current = current or {
        "filename": "current.jsonl",
        "dataset_version": "fixture-current-v1",
        "record_count": 1,
        "bytes": 1,
        "sha256": "1" * 64,
    }
    contract = contract or {
        key: value for key, value in inherited["contract"].items() if key != "format"
    }
    classifications = _classification_counts(row_count)
    assert correios_directory is not None, "refresh document requires the snapshot dir"
    return {
        "format": "opencepgeo-correios-refresh-manifest-v1",
        "status": "offline-candidate-not-approved-for-promotion",
        "dataset_version": dataset_version,
        "inputs": {
            "current_opencepgeo": current,
            "current_release_contract": contract,
            "correios_snapshot": _correios_snapshot(correios_directory),
        },
        "candidate_rows": row_count,
        "classification_counts": classifications,
        "inherited_base_release": inherited,
        "artifacts": {
            normalized.name: {
                "format": "opencepgeo-jsonl-v4",
                "bytes": normalized.stat().st_size,
                "sha256": _sha256(normalized),
            },
            f"opencepgeo-{dataset_version}.sqlite": {
                "format": "opencepgeo-correios-candidate-sqlite-v1",
                "bytes": 1,
                "sha256": "4" * 64,
            },
            "diff.jsonl": {
                "format": "opencepgeo-correios-refresh-diff-v1",
                "bytes": diff.stat().st_size,
                "sha256": _sha256(diff),
            },
            "quality-report.json": {
                "format": "opencepgeo-correios-refresh-quality-v1",
                "bytes": quality.stat().st_size,
                "sha256": _sha256(quality),
            },
        },
    }


def _write_fixture(
    root: Path,
    rows: list[dict[str, object]] | None = None,
    *,
    include_boundary_members: bool = True,
    source_lock_release: str = "fixture-current-v1",
    current_contract_mutator: Callable[[dict[str, object]], None] | None = None,
    inherited_quality_mutator: Callable[[dict[str, object]], None] | None = None,
    snapshot_captured_at: str = "2026-08-11T01:30:00Z",
    snapshot_dnec_published_at: str = "2026-08-11T00:00:00Z",
):
    normalized = root / f"opencepgeo-{DATASET_VERSION}.jsonl"
    fixture_rows = rows or _rows()
    normalized.write_bytes(b"".join(_canonical_row(row) for row in fixture_rows))
    diff = root / "diff.jsonl"
    _write_diff(diff, fixture_rows)
    classifications = _classification_counts(len(fixture_rows))
    snapshot_directory = root / "snapshot"
    _write_correios_snapshot(
        snapshot_directory,
        len(fixture_rows),
        captured_at=snapshot_captured_at,
        dnec_published_at=snapshot_dnec_published_at,
    )
    correios = _correios_snapshot(snapshot_directory)
    refresh_quality = root / "quality-report.json"
    _write_json(
        refresh_quality,
        {
            "format": "opencepgeo-correios-refresh-quality-v1",
            "dataset_version": DATASET_VERSION,
            "candidate_rows": len(fixture_rows),
            "classification_counts": classifications,
            "correios_snapshot": {
                key: value
                for key, value in correios.items()
                if key not in {"directory", "manifest_sha256", "addresses_sha256"}
            },
            "inherited_base_release": _inherited_release(),
            "invariants": {
                "active_database_mutated": False,
                "candidate_rows_equal_located_plus_unresolved": True,
                "correios_snapshot_hash_verified": True,
                "current_input_stable_across_build": True,
                "current_rows_retained": True,
            },
            "located_rows": 1,
            "unresolved_rows": len(fixture_rows) - 1,
            "precision_counts": {"municipality": 1, "osm_postcode": 0},
            "geography_action_counts": _geography_action_counts(len(fixture_rows)),
        },
    )
    ibge = root / "ibge.gpkg"
    make_ibge_gpkg(ibge)
    connection = sqlite3.connect(ibge)
    connection.execute(
        "INSERT INTO localities VALUES (?, ?, ?, ?)",
        (
            "2605459",
            "Distrito Estadual de Fernando de Noronha",
            -3.8547,
            -32.4233,
        ),
    )
    connection.commit()
    connection.close()
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
    source_lock = root / "lock.json"
    sources = []
    for source_id, role, source in (
        ("ibge-fixture", "fixture municipality points", ibge),
        ("ibge-boundaries-fixture", "fixture municipality boundaries", boundaries),
    ):
        source_record = {
            "id": source_id,
            "role": role,
            "required": True,
            "version": "fixture-v1",
            "filename": source.name,
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
            "acquisition": "https",
            "url": f"https://example.invalid/{source.name}",
            "retrieved_at": "2026-08-11T00:00:00Z",
            "attribution": "IBGE fixture",
            "license_status": "test-only",
            "terms_status": "test-only",
        }
        if source is boundaries and include_boundary_members:
            import zipfile

            with zipfile.ZipFile(boundaries) as archive:
                source_record["members"] = {
                    info.filename: {
                        "bytes": info.file_size,
                        "sha256": hashlib.sha256(archive.read(info)).hexdigest(),
                    }
                    for info in archive.infolist()
                }
        sources.append(source_record)
    opencep = root / "opencep-fixture.zip"
    opencep.write_bytes(b"fixture OpenCEP corpus")
    corrections = root / "opencep-corrections.json"
    corrections.write_bytes(b'{"format":"opencepgeo-corrections-v1"}\n')
    sources[:0] = [
        {
            "id": "opencep-fixture",
            "role": "CEP and address corpus",
            "required": True,
            "version": "fixture-v1",
            "filename": opencep.name,
            "bytes": opencep.stat().st_size,
            "sha256": _sha256(opencep),
            "acquisition": "https",
            "url": f"https://example.invalid/{opencep.name}",
            "retrieved_at": "2026-08-11T00:00:00Z",
            "attribution": "OpenCEP fixture",
            "license_status": "test-only",
            "terms_status": "test-only",
        },
        {
            "id": "opencep-corrections-fixture",
            "role": "Audited OpenCEP correction set",
            "required": True,
            "version": "fixture-v1",
            "filename": corrections.name,
            "bytes": corrections.stat().st_size,
            "sha256": _sha256(corrections),
            "acquisition": "repository",
            "local_path": f"sources/{corrections.name}",
            "retrieved_at": "2026-08-11T00:00:00Z",
            "attribution": "OpenCEPGeo fixture correction",
            "license_status": "test-only",
            "terms_status": "test-only",
        },
    ]
    _write_json(
        source_lock,
        {
            "format": "opencepgeo-source-lock-v1",
            "release": source_lock_release,
            "publication_gate": "blocked-fixture-rights-review",
            "sources": sources,
        },
    )
    osm, official_holdout = _write_validation_inputs(root, source_lock)
    repository = Path(__file__).resolve().parents[1]
    enrichment = root / "enrichment-v1.json"
    enrichment.write_bytes((repository / "config/enrichment-v1.json").read_bytes())
    quality = root / "quality-v1.json"
    _write_quality_policy(quality)

    inherited_release = root / "inherited-release"
    inherited_release.mkdir()
    current_normalized = inherited_release / "opencepgeo-fixture-current-v1.jsonl"
    current_row = {**_rows()[0], "dataset_version": "fixture-current-v1"}
    current_normalized.write_bytes(_canonical_row(current_row))
    inherited_source_lock = inherited_release / "source-lock.json"
    inherited_enrichment = inherited_release / "enrichment-config.json"
    inherited_quality_policy = inherited_release / "quality-policy.json"
    shutil.copyfile(source_lock, inherited_source_lock)
    shutil.copyfile(enrichment, inherited_enrichment)
    shutil.copyfile(quality, inherited_quality_policy)
    current_contract = root / "opencepgeo-v4-fixture-current-v1.json"
    builder = {
        "name": "opencepgeo",
        "version": "0.2.0rc2",
        "source_tree_sha256": "b" * 64,
    }
    boundary_members = next(
        source.get("members")
        for source in sources
        if source["id"] == "ibge-boundaries-fixture"
    )
    ibge_record = {
        "filename": ibge.name,
        "bytes": ibge.stat().st_size,
        "sha256": _sha256(ibge),
    }
    boundary_record: dict[str, object] = {
        "filename": boundaries.name,
        "bytes": boundaries.stat().st_size,
        "sha256": _sha256(boundaries),
    }
    if boundary_members is not None:
        boundary_record["members"] = boundary_members
    osm_manifest = osm.with_suffix(".manifest.json")
    osm_document = json.loads(osm_manifest.read_text(encoding="utf-8"))
    osm_record = {
        "artifact": {
            "filename": osm.name,
            "bytes": osm.stat().st_size,
            "sha256": _sha256(osm),
        },
        "manifest": {
            "filename": osm_manifest.name,
            "bytes": osm_manifest.stat().st_size,
            "sha256": _sha256(osm_manifest),
        },
        "source": osm_document["source"],
        "statistics": osm_document["statistics"],
        "publication_gate": osm_document["publication_gate"],
    }
    enrichment_record = {
        "filename": enrichment.name,
        "bytes": enrichment.stat().st_size,
        "sha256": _sha256(enrichment),
        "content": json.loads(enrichment.read_text(encoding="utf-8")),
    }
    quality_record = {
        "filename": quality.name,
        "sha256": _sha256(quality),
        "version": "fixture-quality-v1",
    }
    inherited_build_manifest = inherited_release / "build-manifest.json"
    _write_json(
        inherited_build_manifest,
        {
            "format": "opencepgeo-build-manifest-v2",
            "dataset_version": "fixture-current-v1",
            "builder": builder,
            "inputs": {
                "ibge": ibge_record,
                "municipality_boundaries": boundary_record,
                "opencep": {
                    "filename": opencep.name,
                    "bytes": opencep.stat().st_size,
                    "sha256": _sha256(opencep),
                },
            },
            "configuration": {
                "enrichment": enrichment_record,
                "quality": quality_record,
                "osm_observations": osm_record,
                "observations": None,
                "opencep_corrections": {
                    "filename": corrections.name,
                    "bytes": corrections.stat().st_size,
                    "sha256": _sha256(corrections),
                },
                "osm_boundary_selection": {"method": "fixture-polygon-containment-v1"},
            },
            "source_lock": {
                "filename": source_lock.name,
                "bytes": source_lock.stat().st_size,
                "sha256": _sha256(source_lock),
                "publication_gate": "blocked-fixture-rights-review",
            },
            "artifacts": {
                "normalized": {
                    "filename": "opencepgeo.jsonl",
                    "bytes": current_normalized.stat().st_size,
                    "sha256": _sha256(current_normalized),
                    "format": "opencepgeo-jsonl-v4",
                }
            },
            "sources": sources,
        },
    )
    inherited_quality = inherited_release / "quality-report.json"
    inherited_quality_document: dict[str, object] = {
        "format": "opencepgeo-quality-report-v2",
        "status": "pass",
        "dataset_version": "fixture-current-v1",
        "quality_version": "quality-fixture-current-v1",
        "artifact": {"record_count": 1},
    }
    if inherited_quality_mutator is not None:
        inherited_quality_mutator(inherited_quality_document)
    _write_json(inherited_quality, inherited_quality_document)
    inherited_notice = inherited_release / "NOTICE.md"
    inherited_notice.write_text("fixture attribution notice\n", encoding="utf-8")
    inherited_corrections = inherited_release / "opencep-corrections.json"
    shutil.copyfile(corrections, inherited_corrections)
    inherited_csv = inherited_release / "opencepgeo-fixture-current-v1.csv"
    inherited_csv.write_text("cep\n01001000\n", encoding="utf-8")
    inherited_sqlite = inherited_release / "opencepgeo-fixture-current-v1.sqlite"
    inherited_sqlite.write_bytes(b"fixture packaged sqlite")
    inherited_quality_markdown = inherited_release / "quality-report.md"
    inherited_quality_markdown.write_text("# Fixture quality\n", encoding="utf-8")
    inherited_manifest = inherited_release / "manifest.json"
    release_files = {
        path.name: {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "format": file_format,
        }
        for path, file_format in (
            (inherited_build_manifest, "opencepgeo-build-manifest-v2"),
            (current_normalized, "opencepgeo-jsonl-v4"),
            (inherited_enrichment, "opencepgeo-enrichment-v1"),
            (inherited_quality_policy, "opencepgeo-quality-policy-v2"),
            (inherited_quality, "opencepgeo-quality-report-v2"),
            (inherited_source_lock, "opencepgeo-source-lock-v1"),
            (inherited_notice, "markdown"),
            (inherited_corrections, "opencepgeo-corrections-v1"),
            (inherited_csv, "opencepgeo-csv-v4"),
            (inherited_sqlite, "opencepgeo-sqlite-v4"),
            (inherited_quality_markdown, "markdown"),
        )
    }
    _write_json(
        inherited_manifest,
        {
            "format": "opencepgeo-release-manifest-v2",
            "dataset_version": "fixture-current-v1",
            "release_status": "blocked-private-release-candidate",
            "publication_gate": "blocked-fixture-rights-review",
            "builder": builder,
            "source_lock_sha256": _sha256(inherited_source_lock),
            "quality_attestation": {
                "report_sha256": _sha256(inherited_quality),
                "build_manifest_sha256": _sha256(inherited_build_manifest),
            },
            "files": release_files,
        },
    )
    inherited_checksums = inherited_release / "SHA256SUMS"
    inherited_checksums.write_text(
        "fixture checksums are contract-bound\n", encoding="utf-8"
    )
    current_contract_document: dict[str, object] = {
        "format": "px-opencepgeo-import-contract-v1",
        "manifest_format": "opencepgeo-build-manifest-v2",
        "schema_version": "opencepgeo-sqlite-v4",
        "artifact_format": "opencepgeo-jsonl-v4",
        "evidence_radius_field": "evidence_radius_km",
        "normalized_artifact_path": ["artifacts", "normalized"],
        "statistics_path": ["statistics"],
        "sources_path": ["sources"],
        "source_lock_path": ["source_lock"],
        "builder_path": ["builder"],
        "quality_path": ["configuration", "quality"],
        "builder_required_keys": ["name", "version", "source_tree_sha256"],
        "quality_required_keys": ["version", "sha256"],
        "quality_status_field": "version",
        "quality_pass_value": "quality-fixture-current-v1",
        "coordinate_bounds": {
            "latitude_min": -34.0,
            "latitude_max": 5.5,
            "longitude_min": -74.0,
            "longitude_max": -28.0,
        },
        "require_city": True,
        "require_ibge": True,
        "source_category_pattern": "^[a-z0-9][a-z0-9_.:-]{0,63}$",
        "source_category_max_utf8_bytes": 64,
        "source_categories_sorted": True,
        "max_jsonl_line_bytes": 65536,
        "max_geo_source_count": 16,
        "max_geo_source_serialized_bytes": 2048,
        "row_keys": [
            "cep",
            "prefix",
            "street",
            "complement",
            "unit",
            "neighborhood",
            "city",
            "uf",
            "state",
            "region",
            "ibge",
            "dataset_version",
            "geo",
        ],
        "string_byte_limits": {
            "dataset_version": 128,
            "street": 4096,
            "complement": 4096,
            "unit": 1024,
            "neighborhood": 4096,
            "city": 512,
            "state": 512,
            "region": 512,
            "method": 128,
        },
        "approved_release": {
            "dataset_version": "fixture-current-v1",
            "release_manifest_filename": inherited_manifest.name,
            "release_manifest_bytes": inherited_manifest.stat().st_size,
            "release_manifest_format": "opencepgeo-release-manifest-v2",
            "release_manifest_sha256": _sha256(inherited_manifest),
            "release_status": "blocked-private-release-candidate",
            "publication_gate": "blocked-fixture-rights-review",
            "record_count": 1,
            "artifact_filename": current_normalized.name,
            "builder": builder,
            "auxiliary_files": {
                "SHA256SUMS": {
                    "bytes": inherited_checksums.stat().st_size,
                    "format": "sha256sum",
                    "sha256": _sha256(inherited_checksums),
                }
            },
            "files": release_files,
        },
    }
    if current_contract_mutator is not None:
        current_contract_mutator(current_contract_document)
    _write_json(current_contract, current_contract_document)

    def inherited_identity(path: Path, file_format: str) -> dict[str, object]:
        return {
            "filename": path.name,
            "format": file_format,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }

    inherited = {
        "build_manifest": inherited_identity(
            inherited_build_manifest, "opencepgeo-build-manifest-v2"
        ),
        "builder": builder,
        "contract": inherited_identity(
            current_contract, "px-opencepgeo-import-contract-v1"
        ),
        "dataset_version": "fixture-current-v1",
        "enrichment": inherited_identity(
            inherited_enrichment, "opencepgeo-enrichment-v1"
        ),
        "normalized_artifact": inherited_identity(
            current_normalized, "opencepgeo-jsonl-v4"
        ),
        "publication_gate": "blocked-fixture-rights-review",
        "quality_pass_value": "quality-fixture-current-v1",
        "quality_policy": inherited_identity(
            inherited_quality_policy, "opencepgeo-quality-policy-v2"
        ),
        "quality_report": inherited_identity(
            inherited_quality, "opencepgeo-quality-report-v2"
        ),
        "record_count": 1,
        "release_manifest": inherited_identity(
            inherited_manifest, "opencepgeo-release-manifest-v2"
        ),
        "release_status": "blocked-private-release-candidate",
        "source_lock": inherited_identity(
            inherited_source_lock, "opencepgeo-source-lock-v1"
        ),
    }
    current = {
        "filename": current_normalized.name,
        "dataset_version": "fixture-current-v1",
        "record_count": 1,
        "bytes": current_normalized.stat().st_size,
        "sha256": _sha256(current_normalized),
    }
    contract = {
        key: value for key, value in inherited["contract"].items() if key != "format"
    }
    _write_json(
        refresh_quality,
        {
            "format": "opencepgeo-correios-refresh-quality-v1",
            "dataset_version": DATASET_VERSION,
            "candidate_rows": len(fixture_rows),
            "classification_counts": classifications,
            "correios_snapshot": {
                key: value
                for key, value in correios.items()
                if key not in {"directory", "manifest_sha256", "addresses_sha256"}
            },
            "inherited_base_release": inherited,
            "invariants": {
                "active_database_mutated": False,
                "candidate_rows_equal_located_plus_unresolved": True,
                "correios_snapshot_hash_verified": True,
                "current_input_stable_across_build": True,
                "current_rows_retained": True,
            },
            "located_rows": 1,
            "unresolved_rows": len(fixture_rows) - 1,
            "precision_counts": {"municipality": 1, "osm_postcode": 0},
            "geography_action_counts": _geography_action_counts(len(fixture_rows)),
        },
    )
    refresh_manifest = root / "refresh-manifest.json"
    _write_json(
        refresh_manifest,
        _refresh_document(
            normalized,
            refresh_quality,
            diff,
            DATASET_VERSION,
            len(fixture_rows),
            correios_directory=snapshot_directory,
            inherited=inherited,
            current=current,
            contract=contract,
        ),
    )
    repository = Path(__file__).resolve().parents[1]
    refresh_policy = root / "refresh-policy-v1.json"
    refresh_policy.write_bytes(
        (repository / "config/refresh-policy-v1.json").read_bytes()
    )
    return {
        "normalized_path": normalized,
        "refresh_manifest_path": refresh_manifest,
        "refresh_quality_path": refresh_quality,
        "refresh_diff_path": diff,
        "inherited_release_path": inherited_release,
        "current_release_contract_path": current_contract,
        "source_lock_path": source_lock,
        "ibge_path": ibge,
        "osm_observations_path": osm,
        "municipality_boundaries_path": boundaries,
        "enrichment_config_path": enrichment,
        "quality_config_path": quality,
        "correios_snapshot_path": snapshot_directory,
        "refresh_policy_path": refresh_policy,
        # The fixture inherits a 1-row release and adds 2 rows — a >100% jump
        # by construction, which no real profile allows. The default fixture
        # records an explicit operator override (the documented escape hatch);
        # budget-behaviour tests drive the profiles without it.
        "refresh_profile": "catch-up",
        "refresh_override_budget": "fixture: tiny inherited base inflates fractions",
    }


class NormalizedBuildTests(unittest.TestCase):
    def test_accepts_rc2_style_build_to_package_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            inherited = arguments["inherited_release_path"]
            build_manifest = json.loads(
                (inherited / "build-manifest.json").read_text(encoding="utf-8")
            )
            refresh = json.loads(
                arguments["refresh_manifest_path"].read_text(encoding="utf-8")
            )
            self.assertEqual(
                build_manifest["artifacts"]["normalized"]["filename"],
                "opencepgeo.jsonl",
            )
            self.assertEqual(
                refresh["inputs"]["current_opencepgeo"]["filename"],
                "opencepgeo-fixture-current-v1.jsonl",
            )
            self.assertNotEqual(
                build_manifest["artifacts"]["normalized"]["filename"],
                refresh["inputs"]["current_opencepgeo"]["filename"],
            )
            self.assertEqual(build_manifest["source_lock"]["filename"], "lock.json")
            self.assertEqual(
                refresh["inherited_base_release"]["source_lock"]["filename"],
                "source-lock.json",
            )
            self.assertEqual(
                build_manifest["configuration"]["enrichment"]["filename"],
                "enrichment-v1.json",
            )
            self.assertEqual(
                refresh["inherited_base_release"]["enrichment"]["filename"],
                "enrichment-config.json",
            )
            self.assertEqual(
                build_manifest["configuration"]["quality"]["filename"],
                "quality-v1.json",
            )
            self.assertEqual(
                refresh["inherited_base_release"]["quality_policy"]["filename"],
                "quality-policy.json",
            )
            result = build_database_from_normalized(
                **arguments,
                output_path=root / "out.sqlite",
                normalized_output_path=root / "out.jsonl",
                manifest_path=root / "out.manifest.json",
            )
            self.assertEqual(result["unique_ceps"], 3)

    def test_accepts_complete_pin_207_v3_manifest_and_audit_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = _write_fixture(Path(directory))
            loaded = _load_refresh_manifest(
                arguments["refresh_manifest_path"],
                arguments["normalized_path"],
                arguments["refresh_quality_path"],
                arguments["refresh_diff_path"],
            )
            self.assertEqual(
                loaded[0]["inputs"]["correios_snapshot"]["schema_version"], 3
            )
            self.assertEqual(loaded[0]["candidate_rows"], 3)
            self.assertEqual(loaded[5]["unresolved_rows"], 2)

    def test_refresh_requires_correios_and_upstream_attribution(self):
        self.assertEqual(
            _attribution_tokens(
                {
                    "inputs": {"normalized_refresh": {}},
                    "sources": [{"id": "ibge-fixture"}],
                    "configuration": {"osm_observations": {"artifact": {}}},
                }
            ),
            ["Correios", "IBGE", "OpenCEP", "OpenStreetMap"],
        )

    def test_builds_v4_sqlite_preserving_only_evidence_backed_geography(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            output = root / "opencepgeo.sqlite"
            normalized_output = root / "opencepgeo-final.jsonl"
            manifest = root / "opencepgeo.manifest.json"
            stats = build_database_from_normalized(
                **arguments,
                output_path=output,
                normalized_output_path=normalized_output,
                manifest_path=manifest,
            )

            self.assertEqual(stats["unique_ceps"], 3)
            self.assertEqual(stats["geo_inherited"], 1)
            self.assertEqual(stats["geo_filled_municipality"], 1)
            self.assertEqual(stats["geo_filled_administrative"], 1)
            self.assertEqual(stats["unresolved"], 0)
            self.assertEqual(stats["normalized_sha256"], _sha256(normalized_output))
            self.assertNotEqual(
                stats["normalized_sha256"], _sha256(arguments["normalized_path"])
            )
            self.assertEqual(lookup(output, "01001000")["geo"], _rows()[0]["geo"])
            self.assertEqual(
                lookup(output, "20010000")["geo"]["method"],
                "ibge_municipality_reference",
            )
            noronha = lookup(output, "53990959")["geo"]
            self.assertEqual(
                noronha["method"], "ibge_administrative_locality_aggregate"
            )
            self.assertEqual(noronha["source"], ["ibge-localidades-administrative"])
            self.assertEqual(noronha["precision"], "municipality")
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], "opencepgeo-sqlite-v4")
            self.assertEqual(
                document["inputs"]["normalized_refresh"]["status"],
                "offline-candidate-not-approved-for-promotion",
            )
            self.assertEqual(
                document["artifacts"]["normalized"]["sha256"],
                _sha256(normalized_output),
            )
            self.assertEqual(
                document["inputs"]["normalized_refresh"]["candidate"]["sha256"],
                _sha256(arguments["normalized_path"]),
            )
            metadata = dict(
                sqlite3.connect(output).execute("SELECT key, value FROM metadata")
            )
            self.assertEqual(
                metadata["normalized_refresh_manifest_sha256"],
                _sha256(arguments["refresh_manifest_path"]),
            )
            source_ids = [source["id"] for source in document["sources"]]
            self.assertEqual(
                source_ids,
                [
                    "opencep-fixture",
                    "opencep-corrections-fixture",
                    "ibge-fixture",
                    "ibge-boundaries-fixture",
                    "correios-busca-cep-v3",
                ],
            )
            self.assertIn("opencep", document["inputs"])
            self.assertIsNotNone(document["configuration"]["opencep_corrections"])
            self.assertIsNotNone(document["configuration"]["osm_boundary_selection"])
            # The stored geo for 01001000 is preserved byte-identically, but only
            # because it is proven against the inherited release; the gate records
            # that classification and finds nothing moved off its evidence.
            validation = document["inputs"]["normalized_refresh"][
                "geography_derivation"
            ]["coordinate_validation"]
            self.assertEqual(
                validation["policy"],
                "preserve-or-reproduce-from-pinned-ibge-osm-v1",
            )
            self.assertEqual(validation["non_null_candidate_rows"], 1)
            self.assertEqual(validation["preserved_from_inherited"], 1)
            self.assertEqual(validation["reproduced_from_pinned_evidence"], 0)
            self.assertEqual(validation["suspicious_changes"], 0)
            self.assertEqual(validation["displacement_km"]["count"], 0)
            self.assertEqual(validation["osm_polygon"]["checked"], 0)

    def test_rejects_candidate_coordinate_moved_off_pinned_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            moved = _rows()
            # Move 01001000 off both the inherited release point and every pinned
            # IBGE/OSM reference while keeping the geo object syntactically valid.
            moved[0] = {
                **moved[0],
                "geo": {
                    **moved[0]["geo"],
                    "coordinates": [-50.0, -15.0],
                },
            }
            arguments = _write_fixture(root, rows=moved)
            with self.assertRaises(ValueError) as caught:
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "opencepgeo.sqlite",
                    normalized_output_path=root / "opencepgeo-final.jsonl",
                    manifest_path=root / "opencepgeo.manifest.json",
                )
            self.assertIn("reproduced from the pinned", str(caught.exception))
            # The build must fail closed: no artifacts promoted.
            self.assertFalse((root / "opencepgeo.sqlite").exists())

    def test_coordinate_evidence_reproduces_osm_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ibge = root / "ibge.gpkg"
            make_ibge_gpkg(ibge)
            osm = root / "osm.csv"
            osm.write_text(
                "cep,ibge,latitude,longitude,source\n"
                "01001001,,-23.5505,-46.6333,openstreetmap:node/1\n"
                "01001001,,-23.5510,-46.6335,openstreetmap:node/2\n",
                encoding="utf-8",
            )
            boundaries = root / "boundaries.zip"
            write_municipality_boundaries(
                boundaries,
                [
                    (
                        "3550308",
                        [
                            (-47.0, -24.0),
                            (-46.0, -24.0),
                            (-46.0, -23.0),
                            (-47.0, -23.0),
                        ],
                    )
                ],
            )
            repository = Path(__file__).resolve().parents[1]
            enrichment = database_module.load_enrichment_config(
                repository / "config/enrichment-v1.json"
            )[0]
            municipalities = database_module.load_ibge_municipality_references(ibge)
            estimator = database_module._coordinate_evidence_estimator(
                municipalities, osm, enrichment
            )
            estimate = estimator.estimate("01001001", "3550308")
            self.assertEqual(estimate.precision, "osm_postcode")
            osm_geo = estimate.as_geojson()

            def _row(geo: dict[str, object], version: str) -> dict[str, object]:
                return {
                    "cep": "01001001",
                    "prefix": "01001",
                    "street": "Rua Fixture",
                    "complement": None,
                    "unit": None,
                    "neighborhood": None,
                    "city": "São Paulo",
                    "uf": "SP",
                    "state": "São Paulo",
                    "region": "Sudeste",
                    "ibge": "3550308",
                    "dataset_version": version,
                    "geo": geo,
                }

            inherited_geo = {
                "type": "Point",
                "coordinates": [-46.6333, -23.5505],
                "precision": "municipality",
                "method": "ibge_city_reference_with_locality_dispersion",
                "evidence_count": 1,
                "evidence_radius_km": 0.0,
                "source": ["ibge"],
                "evidence_digest": "sha256:" + "0" * 64,
            }
            inherited = root / "inherited.jsonl"
            inherited.write_bytes(_canonical_row(_row(inherited_geo, "inherited-v1")))
            inherited_record = {
                "bytes": inherited.stat().st_size,
                "sha256": _sha256(inherited),
            }

            def _validate(candidate_geo: dict[str, object]) -> dict[str, object]:
                candidate = root / "candidate.jsonl"
                candidate.write_bytes(
                    _canonical_row(_row(candidate_geo, "candidate-v1"))
                )
                return database_module._validate_candidate_geography_evidence(
                    candidate_path=candidate,
                    candidate_record={
                        "bytes": candidate.stat().st_size,
                        "sha256": _sha256(candidate),
                    },
                    candidate_dataset_version="candidate-v1",
                    inherited_path=inherited,
                    inherited_record=inherited_record,
                    inherited_dataset_version="inherited-v1",
                    osm_observations_path=osm,
                    municipality_boundaries_path=boundaries,
                    boundary_members=None,
                    municipalities=municipalities,
                    administrative={},
                    enrichment=enrichment,
                    max_outside_polygon_fraction=0.0,
                )

            stats = _validate(osm_geo)
            self.assertEqual(stats["non_null_candidate_rows"], 1)
            self.assertEqual(stats["preserved_from_inherited"], 0)
            self.assertEqual(stats["reproduced_from_pinned_evidence"], 1)
            self.assertEqual(stats["reproduced_changed_cep"], 1)
            self.assertEqual(stats["suspicious_changes"], 0)
            self.assertEqual(stats["displacement_km"]["count"], 1)
            self.assertGreater(stats["displacement_km"]["max_km"], 0.0)
            polygon = stats["osm_polygon"]
            self.assertEqual(polygon["checked"], 1)
            self.assertEqual(polygon["interior"] + polygon["boundary"], 1)
            self.assertEqual(polygon["outside"], 0)

            moved = {**osm_geo, "coordinates": [-40.0, -10.0]}
            with self.assertRaises(ValueError) as caught:
                _validate(moved)
            self.assertIn("reproduced from the pinned", str(caught.exception))

    def test_coordinate_evidence_accepts_municipality_point_with_osm_nodes(self):
        # Regression: a candidate may legitimately keep a coarse municipality
        # point for a CEP that also has OSM nodes. Validation must dispatch on the
        # stored precision (municipality -> pinned IBGE reference) rather than
        # re-deciding the tier via the estimator, which would upgrade the CEP to
        # osm_postcode and spuriously reject the point.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ibge = root / "ibge.gpkg"
            make_ibge_gpkg(ibge)  # 3550308 city reference at (-46.6333, -23.5505)
            osm = root / "osm.csv"
            osm.write_text(
                "cep,ibge,latitude,longitude,source\n"
                "01001002,,-23.5505,-46.6333,openstreetmap:node/1\n"
                "01001002,,-23.5510,-46.6335,openstreetmap:node/2\n",
                encoding="utf-8",
            )
            boundaries = root / "boundaries.zip"
            write_municipality_boundaries(
                boundaries,
                [
                    (
                        "3550308",
                        [
                            (-47.0, -24.0),
                            (-46.0, -24.0),
                            (-46.0, -23.0),
                            (-47.0, -23.0),
                        ],
                    )
                ],
            )
            repository = Path(__file__).resolve().parents[1]
            enrichment = database_module.load_enrichment_config(
                repository / "config/enrichment-v1.json"
            )[0]
            municipalities = database_module.load_ibge_municipality_references(ibge)
            reference = municipalities["3550308"].point
            municipality_geo = {
                "type": "Point",
                "coordinates": [reference.longitude, reference.latitude],
                "precision": "municipality",
                "method": "ibge_city_reference_with_locality_dispersion",
                "evidence_count": 1,
                "evidence_radius_km": 0.0,
                "source": ["ibge"],
                "evidence_digest": "sha256:" + "0" * 64,
            }
            row = {
                "cep": "01001002",
                "prefix": "01001",
                "street": None,
                "complement": None,
                "unit": None,
                "neighborhood": None,
                "city": "São Paulo",
                "uf": "SP",
                "state": "São Paulo",
                "region": "Sudeste",
                "ibge": "3550308",
                "dataset_version": "candidate-v1",
                "geo": municipality_geo,
            }
            candidate = root / "candidate.jsonl"
            candidate.write_bytes(_canonical_row(row))
            # inherited release: an earlier, unrelated CEP -> 01001002 is new.
            inherited_row = {**row, "cep": "01000000", "prefix": "01000", "geo": None}
            inherited = root / "inherited.jsonl"
            inherited.write_bytes(
                _canonical_row({**inherited_row, "dataset_version": "inherited-v1"})
            )
            stats = database_module._validate_candidate_geography_evidence(
                candidate_path=candidate,
                candidate_record={
                    "bytes": candidate.stat().st_size,
                    "sha256": _sha256(candidate),
                },
                candidate_dataset_version="candidate-v1",
                inherited_path=inherited,
                inherited_record={
                    "bytes": inherited.stat().st_size,
                    "sha256": _sha256(inherited),
                },
                inherited_dataset_version="inherited-v1",
                osm_observations_path=osm,
                municipality_boundaries_path=boundaries,
                boundary_members=None,
                municipalities=municipalities,
                administrative={},
                enrichment=enrichment,
                max_outside_polygon_fraction=0.0,
            )
            self.assertEqual(stats["suspicious_changes"], 0)
            self.assertEqual(stats["reproduced_from_pinned_evidence"], 1)
            self.assertEqual(stats["reproduced_new_cep"], 1)
            self.assertEqual(stats["osm_polygon"]["checked"], 0)

            # Moving that municipality point off its IBGE reference is rejected.
            moved = {**row, "geo": {**municipality_geo, "coordinates": [-50.0, -15.0]}}
            moved_path = root / "moved.jsonl"
            moved_path.write_bytes(_canonical_row(moved))
            with self.assertRaises(ValueError):
                database_module._validate_candidate_geography_evidence(
                    candidate_path=moved_path,
                    candidate_record={
                        "bytes": moved_path.stat().st_size,
                        "sha256": _sha256(moved_path),
                    },
                    candidate_dataset_version="candidate-v1",
                    inherited_path=inherited,
                    inherited_record={
                        "bytes": inherited.stat().st_size,
                        "sha256": _sha256(inherited),
                    },
                    inherited_dataset_version="inherited-v1",
                    osm_observations_path=osm,
                    municipality_boundaries_path=boundaries,
                    boundary_members=None,
                    municipalities=municipalities,
                    administrative={},
                    enrichment=enrichment,
                    max_outside_polygon_fraction=0.0,
                )

    def test_snapshots_all_consumed_inputs_before_downstream_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            real_load_refresh = database_module._load_refresh_manifest
            real_verify_inherited = database_module._verify_inherited_release
            refresh_originals = [
                arguments[key]
                for key in (
                    "normalized_path",
                    "refresh_manifest_path",
                    "refresh_quality_path",
                    "refresh_diff_path",
                )
            ]
            pinned_originals = [
                arguments[key]
                for key in (
                    "current_release_contract_path",
                    "source_lock_path",
                    "ibge_path",
                    "osm_observations_path",
                    "municipality_boundaries_path",
                    "enrichment_config_path",
                    "quality_config_path",
                )
            ]
            pinned_originals.append(
                arguments["osm_observations_path"].with_suffix(".manifest.json")
            )
            pinned_originals.extend(
                path
                for path in arguments["inherited_release_path"].iterdir()
                if path.is_file()
            )

            def load_after_swap(*args, **kwargs):
                self.assertTrue(
                    all(".opencepgeo-normalized-build-" in str(path) for path in args)
                )
                for path in refresh_originals:
                    path.write_bytes(b"swapped after refresh snapshot")
                return real_load_refresh(*args, **kwargs)

            def verify_after_swap(**kwargs):
                self.assertTrue(
                    all(
                        ".opencepgeo-normalized-build-" in str(path)
                        for key, path in kwargs.items()
                        if key != "refresh"
                    )
                )
                for path in pinned_originals:
                    path.write_bytes(b"swapped after pinned snapshot")
                return real_verify_inherited(**kwargs)

            with (
                mock.patch(
                    "opencepgeo.database._load_refresh_manifest",
                    side_effect=load_after_swap,
                ),
                mock.patch(
                    "opencepgeo.database._verify_inherited_release",
                    side_effect=verify_after_swap,
                ),
            ):
                result = build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )
            self.assertEqual(result["unique_ceps"], 3)

    def test_rejects_quality_precision_distribution_not_in_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = _write_fixture(Path(directory))
            quality_path = arguments["refresh_quality_path"]
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            quality["precision_counts"] = {"municipality": 0, "osm_postcode": 1}
            _write_json(quality_path, quality)
            refresh_path = arguments["refresh_manifest_path"]
            refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
            refresh["artifacts"][quality_path.name].update(
                bytes=quality_path.stat().st_size,
                sha256=_sha256(quality_path),
            )
            _write_json(refresh_path, refresh)
            with self.assertRaisesRegex(ValueError, "candidate geography counts"):
                build_database_from_normalized(
                    **arguments,
                    output_path=Path(directory) / "out.sqlite",
                    normalized_output_path=Path(directory) / "out.jsonl",
                    manifest_path=Path(directory) / "out.manifest.json",
                )

    def test_rejects_non_exact_quality_distribution_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = _write_fixture(Path(directory))
            quality_path = arguments["refresh_quality_path"]
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            del quality["geography_action_counts"]["reassigned_municipality"]
            _write_json(quality_path, quality)
            refresh_path = arguments["refresh_manifest_path"]
            refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
            refresh["artifacts"][quality_path.name].update(
                bytes=quality_path.stat().st_size,
                sha256=_sha256(quality_path),
            )
            _write_json(refresh_path, refresh)
            with self.assertRaisesRegex(ValueError, "quality report disagrees"):
                _load_refresh_manifest(
                    refresh_path,
                    arguments["normalized_path"],
                    quality_path,
                    arguments["refresh_diff_path"],
                )

    def test_rejects_diff_counts_not_matching_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = _write_fixture(Path(directory))
            diff_path = arguments["refresh_diff_path"]
            records = [json.loads(line) for line in diff_path.read_text().splitlines()]
            records[1]["geography_action"] = "assigned_municipality"
            diff_path.write_bytes(
                b"".join(_canonical_row(record) for record in records)
            )
            refresh_path = arguments["refresh_manifest_path"]
            refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
            refresh["artifacts"][diff_path.name].update(
                bytes=diff_path.stat().st_size,
                sha256=_sha256(diff_path),
            )
            _write_json(refresh_path, refresh)
            with self.assertRaisesRegex(ValueError, "diff counts disagree"):
                _load_refresh_manifest(
                    refresh_path,
                    arguments["normalized_path"],
                    arguments["refresh_quality_path"],
                    diff_path,
                )

    def test_rejects_invalid_classification_action_pair_even_when_counts_match(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = _write_fixture(Path(directory))
            diff_path = arguments["refresh_diff_path"]
            records = [json.loads(line) for line in diff_path.read_text().splitlines()]
            records[0]["geography_action"] = "unresolved"
            records[1]["geography_action"] = "preserved"
            diff_path.write_bytes(
                b"".join(_canonical_row(record) for record in records)
            )
            refresh_path = arguments["refresh_manifest_path"]
            refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
            refresh["artifacts"][diff_path.name].update(
                bytes=diff_path.stat().st_size,
                sha256=_sha256(diff_path),
            )
            _write_json(refresh_path, refresh)
            with self.assertRaisesRegex(ValueError, "refresh diff line 1 is invalid"):
                _load_refresh_manifest(
                    refresh_path,
                    arguments["normalized_path"],
                    arguments["refresh_quality_path"],
                    diff_path,
                )

    def test_rejects_inherited_quality_semantics(self):
        for field, value in (
            ("status", "fail"),
            ("dataset_version", "another-release"),
            ("quality_version", "another-quality-policy"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)

                def mutate(document, field=field, value=value):
                    document[field] = value

                arguments = _write_fixture(root, inherited_quality_mutator=mutate)
                with self.assertRaisesRegex(
                    ValueError, "inherited release manifests disagree"
                ):
                    build_database_from_normalized(
                        **arguments,
                        output_path=root / "out.sqlite",
                        normalized_output_path=root / "out.jsonl",
                        manifest_path=root / "out.manifest.json",
                    )

    def test_repeated_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            artifacts = []
            for name in ("one", "two"):
                target = root / name
                target.mkdir()
                output = target / "opencepgeo.sqlite"
                normalized_output = target / "opencepgeo-final.jsonl"
                manifest = target / "opencepgeo.manifest.json"
                build_database_from_normalized(
                    **arguments,
                    output_path=output,
                    normalized_output_path=normalized_output,
                    manifest_path=manifest,
                )
                artifacts.append(
                    (
                        output.read_bytes(),
                        normalized_output.read_bytes(),
                        manifest.read_bytes(),
                    )
                )
            self.assertEqual(artifacts[0], artifacts[1])

    def test_rejects_inconsistent_v3_raw_audit_relations(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments = _write_fixture(Path(directory))
            path = arguments["refresh_manifest_path"]
            document = json.loads(path.read_text(encoding="utf-8"))
            document["inputs"]["correios_snapshot"]["source_total_elements"] += 1
            _write_json(path, document)
            with self.assertRaisesRegex(ValueError, "inconsistent Correios counts"):
                _load_refresh_manifest(
                    path,
                    arguments["normalized_path"],
                    arguments["refresh_quality_path"],
                    arguments["refresh_diff_path"],
                )

    def test_rejects_non_exact_v3_nested_count_keys(self):
        cases = (
            ("cep_type_counts", "made_up_type"),
            ("raw_cep_type_counts", "made_up_type"),
            ("validity_counts", "unknown"),
            ("raw_validity_counts", "unknown"),
            ("ibge_resolution_counts", "made_up_resolution"),
        )
        for field, made_up_key in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                arguments = _write_fixture(Path(directory))
                path = arguments["refresh_manifest_path"]
                document = json.loads(path.read_text(encoding="utf-8"))
                counts = document["inputs"]["correios_snapshot"][field]
                existing = next(iter(counts))
                counts[made_up_key] = counts.pop(existing)
                _write_json(path, document)
                with self.assertRaisesRegex(
                    ValueError, "invalid Correios snapshot provenance"
                ):
                    _load_refresh_manifest(
                        path,
                        arguments["normalized_path"],
                        arguments["refresh_quality_path"],
                        arguments["refresh_diff_path"],
                    )

    def test_rejects_partial_and_cross_source_classification_counts(self):
        invalid_counts = (
            {"added": 3},
            {
                "added": 1,
                "address_changed": 0,
                "ibge_changed": 0,
                "missing_from_source": 0,
                "source_link_conflict": 0,
                "unchanged": 2,
            },
            {
                "added": 2,
                "address_changed": 0,
                "ibge_changed": 0,
                "missing_from_source": 1,
                "source_link_conflict": 0,
                "unchanged": 0,
            },
        )
        for counts in invalid_counts:
            with (
                self.subTest(counts=counts),
                tempfile.TemporaryDirectory() as directory,
            ):
                arguments = _write_fixture(Path(directory))
                path = arguments["refresh_manifest_path"]
                document = json.loads(path.read_text(encoding="utf-8"))
                document["classification_counts"] = counts
                _write_json(path, document)
                with self.assertRaisesRegex(
                    ValueError, "invalid classification counts"
                ):
                    _load_refresh_manifest(
                        path,
                        arguments["normalized_path"],
                        arguments["refresh_quality_path"],
                        arguments["refresh_diff_path"],
                    )

    def test_rejects_semantically_tampered_current_release_contract(self):
        def mutate(document: dict[str, object]) -> None:
            document["schema_version"] = "opencepgeo-sqlite-v3"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root, current_contract_mutator=mutate)
            with self.assertRaisesRegex(ValueError, "invalid import contract"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_current_contract_cross_linked_to_another_release(self):
        def mutate(document: dict[str, object]) -> None:
            approved = document["approved_release"]
            assert isinstance(approved, dict)
            approved["artifact_filename"] = "opencepgeo-other-release.jsonl"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root, current_contract_mutator=mutate)
            with self.assertRaisesRegex(ValueError, "disagrees with inherited"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_contract_full_file_map_tamper(self):
        def mutate(document: dict[str, object]) -> None:
            approved = document["approved_release"]
            assert isinstance(approved, dict)
            files = approved["files"]
            assert isinstance(files, dict)
            notice = files["NOTICE.md"]
            assert isinstance(notice, dict)
            notice["sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root, current_contract_mutator=mutate)
            with self.assertRaisesRegex(ValueError, "file map disagrees"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_tampered_contract_auxiliary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            checksum_file = arguments["inherited_release_path"] / "SHA256SUMS"
            checksum_file.write_bytes(b"tampered auxiliary bytes")
            with self.assertRaisesRegex(ValueError, "auxiliary file disagrees"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_tampered_noncore_approved_release_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            notice = arguments["inherited_release_path"] / "NOTICE.md"
            notice.write_bytes(b"tampered inherited notice")
            with self.assertRaisesRegex(ValueError, "approved file disagrees"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_normalized_cli_returns_json_error_without_traceback(self):
        arguments = [
            "build-from-normalized",
            "--normalized",
            "candidate.jsonl",
            "--normalized-output",
            "output.jsonl",
            "--refresh-manifest",
            "refresh.json",
            "--refresh-quality",
            "quality.json",
            "--refresh-diff",
            "diff.jsonl",
            "--inherited-release",
            "release",
            "--current-release-contract",
            "contract.json",
            "--source-lock",
            "lock.json",
            "--ibge",
            "ibge.zip",
            "--osm-observations",
            "osm.csv",
            "--municipality-boundaries",
            "boundaries.zip",
            "--correios-snapshot",
            "snapshot",
            "--output",
            "output.sqlite",
            "--manifest",
            "manifest.json",
        ]
        for error in (
            ValueError("fixture validation failure"),
            OSError("fixture input failure"),
            RuntimeError("fixture publication failure"),
        ):
            with self.subTest(error=type(error).__name__):
                stderr = io.StringIO()
                with (
                    mock.patch(
                        "opencepgeo.cli.build_database_from_normalized",
                        side_effect=error,
                    ),
                    redirect_stderr(stderr),
                ):
                    status = cli_main(arguments)
                self.assertEqual(status, 2)
                self.assertEqual(json.loads(stderr.getvalue()), {"error": str(error)})
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_rejects_candidate_version_source_lock_masquerade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root, source_lock_release=DATASET_VERSION)
            with self.assertRaisesRegex(ValueError, "source lock release"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_tampered_inherited_release_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            inherited = arguments["inherited_release_path"] / "build-manifest.json"
            inherited.write_bytes(inherited.read_bytes() + b" ")
            with self.assertRaisesRegex(
                ValueError, "approved file disagrees for build-manifest"
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_missing_locked_boundary_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root, include_boundary_members=False)
            with self.assertRaisesRegex(ValueError, "require member identities"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_output_collision_with_osm_companion_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            osm_manifest = arguments["osm_observations_path"].with_suffix(
                ".manifest.json"
            )
            before = osm_manifest.read_bytes()
            with self.assertRaisesRegex(ValueError, "collides with an input"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=osm_manifest,
                    force=True,
                )
            self.assertEqual(osm_manifest.read_bytes(), before)

    def test_rejects_non_regular_force_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            output = root / "out.sqlite"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "regular file"):
                build_database_from_normalized(
                    **arguments,
                    output_path=output,
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                    force=True,
                )

    def test_private_staging_precedes_sqlite_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            real_connect = sqlite3.connect
            observed: list[Path] = []

            def checked_connect(database, *args, **kwargs):
                candidate = (
                    Path(database) if isinstance(database, (str, Path)) else None
                )
                if candidate is not None and candidate.name == "database.sqlite":
                    observed.append(candidate)
                    self.assertFalse(candidate.exists())
                    self.assertFalse(candidate.is_symlink())
                    self.assertEqual(
                        stat.S_IMODE(candidate.parent.stat().st_mode), 0o700
                    )
                return real_connect(database, *args, **kwargs)

            with mock.patch(
                "opencepgeo.database.sqlite3.connect", side_effect=checked_connect
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )
            self.assertEqual(len(observed), 1)

    def test_group_publication_rolls_back_second_and_third_link_failures(self):
        cases = (
            (2, {"sqlite": b"old sqlite", "manifest": b"old manifest"}),
            (3, {"normalized": b"old normalized"}),
        )
        for failure_index, existing in cases:
            with (
                self.subTest(failure_index=failure_index),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                arguments = _write_fixture(root)
                targets = {
                    "sqlite": root / "out.sqlite",
                    "normalized": root / "out.jsonl",
                    "manifest": root / "out.manifest.json",
                }
                for name, payload in existing.items():
                    targets[name].write_bytes(payload)
                real_link = __import__("os").link
                publication_links = 0

                def fail_publication(source, target, **kwargs):
                    nonlocal publication_links
                    publication_links += 1
                    if publication_links == failure_index:
                        raise OSError("injected publication failure")
                    return real_link(source, target, **kwargs)

                with (
                    mock.patch(
                        "opencepgeo.database.os.link", side_effect=fail_publication
                    ),
                    self.assertRaisesRegex(OSError, "injected publication failure"),
                ):
                    build_database_from_normalized(
                        **arguments,
                        output_path=targets["sqlite"],
                        normalized_output_path=targets["normalized"],
                        manifest_path=targets["manifest"],
                        force=True,
                    )
                for name, target in targets.items():
                    if name in existing:
                        self.assertEqual(target.read_bytes(), existing[name])
                    else:
                        self.assertFalse(target.exists())

    def test_force_false_never_clobbers_targets_appearing_during_build(self):
        for appearance_index in (1, 2, 3):
            with (
                self.subTest(appearance_index=appearance_index),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                arguments = _write_fixture(root)
                targets = (
                    root / "out.sqlite",
                    root / "out.jsonl",
                    root / "out.manifest.json",
                )
                real_link = __import__("os").link
                link_count = 0
                appeared = b"concurrently created"

                def concurrent_link(source, target, **kwargs):
                    nonlocal link_count
                    link_count += 1
                    if link_count == appearance_index:
                        (root / target).write_bytes(appeared)
                    return real_link(source, target, **kwargs)

                with (
                    mock.patch(
                        "opencepgeo.database.os.link", side_effect=concurrent_link
                    ),
                    self.assertRaises(FileExistsError),
                ):
                    build_database_from_normalized(
                        **arguments,
                        output_path=targets[0],
                        normalized_output_path=targets[1],
                        manifest_path=targets[2],
                    )
                for index, target in enumerate(targets, start=1):
                    if index == appearance_index:
                        self.assertEqual(target.read_bytes(), appeared)
                    else:
                        self.assertFalse(target.exists())

    def test_incomplete_rollback_preserves_recoverable_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            targets = {
                "sqlite": root / "out.sqlite",
                "normalized": root / "out.jsonl",
                "manifest": root / "out.manifest.json",
            }
            originals = {
                "sqlite": b"old sqlite",
                "normalized": b"old normalized",
                "manifest": b"old manifest",
            }
            for name, payload in originals.items():
                targets[name].write_bytes(payload)
            real_link = __import__("os").link
            real_replace = __import__("os").replace

            def fail_publication(source, target, **kwargs):
                if Path(source).name == "normalized.jsonl":
                    raise OSError("injected publication failure")
                return real_link(source, target, **kwargs)

            def fail_restore(source, target, **kwargs):
                if (
                    source == "backup-1-out.jsonl"
                    and target == targets["normalized"].name
                ):
                    raise OSError("injected restore failure")
                return real_replace(source, target, **kwargs)

            with (
                mock.patch("opencepgeo.database.os.link", side_effect=fail_publication),
                mock.patch("opencepgeo.database.os.replace", side_effect=fail_restore),
                self.assertRaisesRegex(RuntimeError, "recover preserved backups"),
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=targets["sqlite"],
                    normalized_output_path=targets["normalized"],
                    manifest_path=targets["manifest"],
                    force=True,
                )
            self.assertEqual(targets["sqlite"].read_bytes(), originals["sqlite"])
            self.assertEqual(targets["manifest"].read_bytes(), originals["manifest"])
            self.assertFalse(targets["normalized"].exists())
            recovery_directories = list(root.glob(".opencepgeo-backup-*"))
            self.assertEqual(len(recovery_directories), 1)
            preserved = recovery_directories[0] / "backup-1-out.jsonl"
            self.assertEqual(preserved.read_bytes(), originals["normalized"])
            self.assertEqual(
                stat.S_IMODE(recovery_directories[0].stat().st_mode), 0o700
            )

    def test_symlinked_output_parent_retarget_fails_without_split_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            output_parent = root / "current-output"
            output_parent.symlink_to(first, target_is_directory=True)
            real_link = __import__("os").link
            links = 0

            def retarget_during_first_link(source, target, **kwargs):
                nonlocal links
                result = real_link(source, target, **kwargs)
                links += 1
                if links == 1:
                    output_parent.unlink()
                    output_parent.symlink_to(second, target_is_directory=True)
                return result

            with (
                mock.patch(
                    "opencepgeo.database.os.link",
                    side_effect=retarget_during_first_link,
                ),
                self.assertRaisesRegex(RuntimeError, "output directory changed"),
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=output_parent / "out.sqlite",
                    normalized_output_path=output_parent / "out.jsonl",
                    manifest_path=output_parent / "out.manifest.json",
                )
            for directory_path in (first, second):
                for name in ("out.sqlite", "out.jsonl", "out.manifest.json"):
                    self.assertFalse((directory_path / name).exists())

    def test_force_concurrent_target_after_backup_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            targets = (
                root / "out.sqlite",
                root / "out.jsonl",
                root / "out.manifest.json",
            )
            originals = (b"old sqlite", b"old normalized", b"old manifest")
            for target, payload in zip(targets, originals, strict=True):
                target.write_bytes(payload)
            real_link = __import__("os").link
            link_count = 0
            concurrent = b"concurrently created after backup"

            def appear_after_backup(source, target, **kwargs):
                nonlocal link_count
                link_count += 1
                if link_count == 2:
                    (root / target).write_bytes(concurrent)
                return real_link(source, target, **kwargs)

            with (
                mock.patch(
                    "opencepgeo.database.os.link", side_effect=appear_after_backup
                ),
                self.assertRaisesRegex(RuntimeError, "rollback was incomplete"),
            ):
                build_database_from_normalized(
                    **arguments,
                    output_path=targets[0],
                    normalized_output_path=targets[1],
                    manifest_path=targets[2],
                    force=True,
                )
            self.assertEqual(targets[0].read_bytes(), originals[0])
            self.assertEqual(targets[1].read_bytes(), concurrent)
            self.assertEqual(targets[2].read_bytes(), originals[2])
            recovery_directories = list(root.glob(".opencepgeo-backup-*"))
            self.assertEqual(len(recovery_directories), 1)
            self.assertEqual(
                (recovery_directories[0] / "backup-1-out.jsonl").read_bytes(),
                originals[1],
            )

    def test_normalized_build_packages_and_verifies_as_a_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            official_holdout = root / "official.csv"
            built = root / "built"
            built.mkdir()
            database = built / f"opencepgeo-{DATASET_VERSION}.sqlite"
            normalized = built / f"opencepgeo-{DATASET_VERSION}.jsonl"
            build_manifest = built / "build-manifest.json"
            build_database_from_normalized(
                **arguments,
                output_path=database,
                normalized_output_path=normalized,
                manifest_path=build_manifest,
            )
            quality = build_quality_report(
                database_path=database,
                build_manifest_path=build_manifest,
                ibge_path=arguments["ibge_path"],
                osm_observations_path=arguments["osm_observations_path"],
                official_holdout_path=official_holdout,
                official_holdout_source_id="official-fixture-v1",
                municipality_boundaries_path=arguments["municipality_boundaries_path"],
                enrichment_config_path=arguments["enrichment_config_path"],
                quality_policy_path=arguments["quality_config_path"],
            )
            self.assertEqual(quality["status"], "pass")
            quality_report = built / "quality-report.json"
            quality_markdown = built / "quality-report.md"
            write_quality_report(quality, quality_report)
            quality_markdown.write_text(
                quality_report_markdown(quality), encoding="utf-8"
            )
            notice = built / "NOTICE.md"
            notice.write_text(
                "Correios\\nOpenCEP\\nIBGE\\nOpenStreetMap\\n", encoding="utf-8"
            )
            release = root / "release"
            result = package_release(
                database_path=database,
                normalized_path=normalized,
                build_manifest_path=build_manifest,
                quality_report_path=quality_report,
                quality_markdown_path=quality_markdown,
                notice_path=notice,
                source_lock_path=arguments["source_lock_path"],
                enrichment_config_path=arguments["enrichment_config_path"],
                quality_policy_path=arguments["quality_config_path"],
                ibge_path=arguments["ibge_path"],
                osm_observations_path=arguments["osm_observations_path"],
                official_holdout_path=official_holdout,
                official_holdout_source_id="official-fixture-v1",
                municipality_boundaries_path=arguments["municipality_boundaries_path"],
                corrections_path=root / "opencep-corrections.json",
                output_directory=release,
            )
            self.assertEqual(
                result["release_status"], "blocked-private-release-candidate"
            )
            self.assertEqual(
                verify_release(release)["dataset_version"], DATASET_VERSION
            )

    def test_rejects_tampered_noncanonical_and_unsorted_inputs_atomically(self):
        cases = {
            "noncanonical": lambda rows: json.dumps(rows[0]).encode("utf-8") + b"\n",
            "unsorted": lambda rows: b"".join(
                _canonical_row(row) for row in reversed(rows)
            ),
            "duplicate": lambda rows: _canonical_row(rows[0]) * 2,
            "missing-terminal-newline": lambda rows: _canonical_row(rows[0]).rstrip(
                b"\n"
            ),
            "wrong-version": lambda rows: _canonical_row(
                {**rows[0], "dataset_version": "different-version"}
            ),
            "outside-brazil": lambda rows: _canonical_row(
                {
                    **rows[0],
                    "geo": {
                        **rows[0]["geo"],
                        "coordinates": [0.0, 0.0],
                    },
                }
            ),
            "oversized": lambda rows: b"{" + b" " * 65_536 + b"}\n",
        }
        for name, payload in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                arguments = _write_fixture(root)
                normalized = arguments["normalized_path"]
                normalized.write_bytes(payload(_rows()))
                refresh_path = arguments["refresh_manifest_path"]
                refresh = json.loads(refresh_path.read_text(encoding="utf-8"))
                refresh["artifacts"][normalized.name][
                    "bytes"
                ] = normalized.stat().st_size
                refresh["artifacts"][normalized.name]["sha256"] = _sha256(normalized)
                _write_json(refresh_path, refresh)
                output = root / "opencepgeo.sqlite"
                normalized_output = root / "opencepgeo-final.jsonl"
                manifest = root / "opencepgeo.manifest.json"
                output.write_bytes(b"old sqlite")
                normalized_output.write_bytes(b"old normalized")
                manifest.write_bytes(b"old manifest")
                with self.assertRaises(ValueError):
                    build_database_from_normalized(
                        **arguments,
                        output_path=output,
                        normalized_output_path=normalized_output,
                        manifest_path=manifest,
                        force=True,
                    )
                self.assertEqual(output.read_bytes(), b"old sqlite")
                self.assertEqual(normalized_output.read_bytes(), b"old normalized")
                self.assertEqual(manifest.read_bytes(), b"old manifest")

    def test_rejects_symlinked_normalized_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            normalized = arguments["normalized_path"]
            backing = root / "candidate-backing.jsonl"
            normalized.rename(backing)
            normalized.symlink_to(backing)
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )

    def test_rejects_refresh_hash_mismatch_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = _write_fixture(root)
            arguments["normalized_path"].write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ValueError, "does not match refresh manifest"):
                build_database_from_normalized(
                    **arguments,
                    output_path=root / "out.sqlite",
                    normalized_output_path=root / "out.jsonl",
                    manifest_path=root / "out.manifest.json",
                )


if __name__ == "__main__":
    unittest.main()
