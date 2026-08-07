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
2. `osm_postcode`: robust centroid of local OSM nodes with an explicit CEP tag.
3. `observed_cep_prefix`: robust centroid of at least three points sharing the
   first five CEP digits **and** the same IBGE municipality. Prefix estimates
   are rejected when their radius exceeds the configured safety threshold.
4. `municipality`: official IBGE city/locality point joined through the
   seven-digit municipality code.
5. `unresolved`: address data is retained, but no coordinate is invented.

Every located row includes `precision`, `method`, `evidence_count`,
`uncertainty_km`, `geo_source`, and `dataset_version`.

## Data inputs

- [OpenCEP release](https://github.com/SeuAliado/OpenCEP/releases) (`v1.zip`):
  CEP, address, UF, and IBGE municipality code.
- [IBGE Localidades do Brasil](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/estrutura-territorial/27385-localidades.html)
  GeoPackage: official municipality/locality reference points.
- Optional observations CSV: trusted CEP points from first-party datasets such
  as already-geocoded stores.

OpenCEPGeo does not redistribute these inputs. Review [NOTICE.md](NOTICE.md)
before publishing derived artifacts.

The exact private-build inputs are pinned by size and SHA-256 in
[`sources/lock.json`](sources/lock.json). Fetch and re-verify required inputs
without silently accepting upstream changes:

```bash
opencepgeo sources fetch --lock sources/lock.json --input-dir data/locked
opencepgeo sources verify --lock sources/lock.json --input-dir data/locked
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
  --source-lock sources/lock.json \
  --config config/enrichment-v1.json \
  --output out/opencepgeo.sqlite

opencepgeo lookup --database out/opencepgeo.sqlite 01001000
```

The first-party observations CSV has this contract:

```csv
cep,ibge,latitude,longitude,source
01001000,3550308,-23.5505,-46.6333,first-party-store
```

`ibge` is required for prefix aggregation. Exact CEP observations remain
usable without it.

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
    "uncertainty_km": 0.0,
    "source": ["first-party-store"]
  },
  "dataset_version": "2026.2.1-rc1"
}
```

GeoJSON coordinate order is longitude, latitude.

## Validation

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

See [ADR 0001](docs/adr/0001-offline-centroid-pipeline.md) for the design and
trade-offs. Downstream bulk consumers should follow the
[Price Index integration contract](docs/price-index-integration.md).
The three-artifact output and reproducibility identity are specified by the
[deterministic build contract](docs/build-contract.md).
Optional first-party and local OSM inputs follow the fail-closed
[offline enrichment contract](docs/enrichment.md).
