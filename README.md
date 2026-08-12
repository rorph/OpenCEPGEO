# OpenCEPGeo

OpenCEPGeo builds a versioned, fully offline CEP-to-centroid database for
Brazil. It preserves OpenCEP address fields and adds an explicit geographic
estimate with provenance and precision.

The project does **not** call BrasilAPI, ViaCEP, Correios, OpenCEP, or public
Nominatim at lookup time. Consumers query a local SQLite artifact.

## Why

CEP datasets generally contain address and IBGE municipality identifiers, but
not coordinates. Treating every geocode as exact hides potentially large
errors. OpenCEPGeo resolves each valid CEP through a documented hierarchy:

1. `observed_cep`: robust centroid of trusted points observed at that CEP.
2. `osm_postcode`: robust centroid of local OSM nodes with an explicit CEP tag,
   retained only when each point is inside or on the locked official polygon
   for the CEP's target municipality. The configured 250 km distance to the
   IBGE locality reference point remains an independent coarse backstop.
3. `observed_cep_prefix`: robust centroid of at least three points sharing the
   first five CEP digits **and** the same IBGE municipality. Prefix estimates
   are rejected when their radius exceeds the configured safety threshold.
4. `municipality`: official IBGE city/locality point joined through the
   seven-digit municipality code.
5. `unresolved`: address data is retained, but no coordinate is invented.

Every located row includes `precision`, `method`, `evidence_count`,
`evidence_radius_km`, bounded source categories, a fixed `evidence_digest`, and
`dataset_version`. Individual OSM node IDs are not repeated in each row; the
build manifest checksums the evidence dataset. `evidence_radius_km` is the
spread of retained evidence around its centroid, not a calibrated position
error or confidence bound.

## Data inputs

- [OpenCEP release](https://github.com/SeuAliado/OpenCEP/releases) (`v1.zip`):
  CEP, address, UF, and IBGE municipality code.
- [IBGE Localidades do Brasil](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/estrutura-territorial/27385-localidades.html)
  GeoPackage: official municipality/locality reference points.
- [IBGE Malha Municipal 2024](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/malhas-territoriais/15774-malhas.html):
  official polygons used to reject OSM points outside the CEP's target
  municipality before they can influence an artifact.
- Optional observations CSV: trusted CEP points from first-party datasets such
  as already-geocoded stores.
- An offline Correios DNEC refresh candidate may replace the OpenCEP ingestion
  step only through `build-from-normalized`. Its canonical JSONL v4 bytes and
  refresh manifest are both checksum-bound; the refresh tool's candidate
  SQLite is never promoted.

OpenCEPGeo does not redistribute these inputs. Review [NOTICE.md](NOTICE.md)
before publishing derived artifacts.

The exact private-build inputs are pinned by size and SHA-256 in
[`sources/lock.json`](sources/lock.json). Fetch and re-verify required inputs
without silently accepting upstream changes:

```bash
opencepgeo sources fetch --lock sources/lock.json --input-dir data/locked
opencepgeo sources verify --lock sources/lock.json --input-dir data/locked

# Production OSM builds also require these checksum-locked optional inputs.
opencepgeo sources fetch --lock sources/lock.json --input-dir data/locked \
  --source ibge-municipios-2024 --source geofabrik-brazil-260806
opencepgeo sources verify --lock sources/lock.json --input-dir data/locked \
  --source ibge-municipios-2024 --source geofabrik-brazil-260806
```

See the [source provenance and publication gate](docs/source-provenance.md)
before selecting optional observations/OSM inputs or distributing artifacts.

## Quick start

Requires Python 3.11 or newer and has no runtime Python dependencies.

```bash
python -m pip install -e .

opencepgeo build \
  --opencep data/locked/opencep-2.0.1-v1.zip \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --municipality-boundaries data/locked/BR_Municipios_2024.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --source-lock sources/lock.json \
  --config config/enrichment-v1.json \
  --quality-config config/quality-v1.json \
  --output out/opencepgeo.sqlite

opencepgeo lookup --database out/opencepgeo.sqlite 01001000
```

To build from an independently reviewed Correios refresh candidate while
preserving every existing point and filling only null geography from the
pinned IBGE locality input:

```bash
opencepgeo build-from-normalized \
  --normalized data/candidate/opencepgeo-2026.2.1-rc3.jsonl \
  --normalized-output out/opencepgeo-2026.2.1-rc3.jsonl \
  --refresh-manifest data/candidate/manifest.json \
  --refresh-quality data/candidate/quality-report.json \
  --refresh-diff data/candidate/diff.jsonl \
  --inherited-release data/releases/2026.2.1-rc2 \
  --current-release-contract data/contracts/opencepgeo-v4-2026.2.1-rc2.json \
  --source-lock sources/lock.json \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --municipality-boundaries data/locked/BR_Municipios_2024.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --config config/enrichment-v1.json \
  --quality-config config/quality-v1.json \
  --output out/opencepgeo-2026.2.1-rc3.sqlite \
  --manifest out/build-manifest.json
```

This path is fail-closed: it requires the refresh status and version contract,
the exact manifest-bound JSONL basename/size/SHA-256, canonical sorted unique
rows, every v4 field and bounded line, all supporting input hashes, the normal
quality gate, SQLite integrity, and a second streaming semantic comparison.
The PIN-207 JSONL remains immutable input evidence. The generated JSONL is a
distinct release artifact and is byte-for-byte equivalent to the final SQLite.

## Internal lookup service

Consumers that should not mount SQLite directly can run the data-agnostic
internal service image over one immutable, read-only artifact:

```bash
docker build -t opencepgeo:local .
```

The image requires `OPENCEPGEO_DATABASE_SHA256` and
`OPENCEPGEO_DATASET_VERSION` before it becomes ready. Do not publish a host
port. The complete read-only mount, internal-network, replacement, and rollback
contract is in the [service runbook](docs/service-runbook.md); the boundary
decision is recorded in [ADR 0002](docs/adr/0002-internal-lookup-service.md).

The production first-party observations CSV has this contract:

```csv
cep,ibge,latitude,longitude,source,evidence_id
01001000,3550308,-23.5505,-46.6333,first-party-store,store-location:123
```

`ibge` is required for prefix aggregation. Exact CEP observations remain
usable without it. Every production first-party row requires a stable,
source-owned `evidence_id`; duplicate identities with different data fail the
build. Coordinate-derived identities are supported only by the lower-level
loader for legacy or synthetic fixtures.

## Output contract

The SQLite database contains one `cep_geo` row per valid OpenCEP record. A
lookup is represented as:

```json
{
  "cep": "01001000",
  "uf": "SP",
  "city": "São Paulo",
  "ibge": "3550308",
  "geo": {
    "type": "Point",
    "coordinates": [-46.6333, -23.5505],
    "precision": "observed_cep",
    "method": "robust_median_first_party",
    "evidence_count": 1,
    "evidence_radius_km": 0.0,
    "source": ["first-party-store"],
    "evidence_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  },
  "dataset_version": "2026.2.1-rc3"
}
```

GeoJSON coordinate order is longitude, latitude.

## Validation

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Production artifacts are also checked against versioned geographic,
count/coverage, consistency, two complementary deterministic OSM cohorts, and
a BA-only independent official pilot. See the
[quality calibration contract](docs/quality.md).

See [ADR 0001](docs/adr/0001-offline-centroid-pipeline.md) for the design and
trade-offs. Downstream bulk consumers should follow the
[Price Index integration contract](docs/price-index-integration.md).
The three-artifact output and reproducibility identity are specified by the
[deterministic build contract](docs/build-contract.md).
Optional first-party and local OSM inputs follow the fail-closed
[offline enrichment contract](docs/enrichment.md).
Validated artifacts are assembled and checked through the
[private release-candidate contract](docs/release.md).
