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
2. `observed_cep_prefix`: robust centroid of at least three points sharing the
   first five CEP digits **and** the same IBGE municipality. Prefix estimates
   are rejected when their radius exceeds the configured safety threshold.
3. `municipality`: official IBGE city/locality point joined through the
   seven-digit municipality code.
4. `unresolved`: address data is retained, but no coordinate is invented.

Every located row includes `precision`, `sample_size`, `radius_km`, and
`geo_source`.

## Data inputs

- [OpenCEP release](https://github.com/SeuAliado/OpenCEP/releases) (`v1.zip`):
  CEP, address, UF, and IBGE municipality code.
- [IBGE Localidades do Brasil](https://www.ibge.gov.br/geociencias/organizacao-do-territorio/estrutura-territorial/27385-localidades.html)
  GeoPackage: official municipality/locality reference points.
- Optional observations CSV: trusted CEP points from first-party datasets such
  as already-geocoded stores.

OpenCEPGeo does not redistribute these inputs. Review [NOTICE.md](NOTICE.md)
before publishing derived artifacts.

## Quick start

Requires Python 3.11 or newer and has no runtime Python dependencies.

```bash
python -m pip install -e .

opencepgeo build \
  --opencep data/v1.zip \
  --ibge data/BR_localidades_2022.gpkg \
  --observations data/observations.csv \
  --source-version opencep-2.0.1+ibge-localidades-2022 \
  --output out/opencepgeo.sqlite

opencepgeo lookup --database out/opencepgeo.sqlite 01001000
```

The observations CSV has this contract:

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
    "sample_size": 1,
    "radius_km": 0.0,
    "source": ["first-party-store"]
  }
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

