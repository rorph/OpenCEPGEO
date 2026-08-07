# Quality calibration and regression gate

`config/quality-v1.json` is the versioned release policy. A production build
fails before publishing any output when it falls outside the configured Brazil
bounds, loses a material number of records or located rows, exceeds the
unresolved ceiling, omits a UF, introduces an unknown precision label, or
creates an IBGE/UF or municipality conflict. SQLite's primary key and the
input/unique count check independently reject duplicate CEPs.

Different historical spellings attached to the same IBGE code are counted as
non-fatal `municipality_label_variants`; the code and UF remain the geographic
identity. A single IBGE code spanning multiple UFs is a failing municipality
conflict.

Municipality and prefix estimates remain explicitly named `municipality` and
`observed_cep_prefix`; neither is represented as exact. Bounds include the
Brazilian mainland and Atlantic islands and were checked against the pinned
IBGE Localidades source.

## Validation cohorts

The broad cohort uses an immutable SHA-256 modulus split of explicit-postcode
OSM nodes. The selected 20% are excluded from the estimator used to predict
their CEPs. This prevents an evaluated node from contributing to its own
centroid. It supplies UF, address-class proxy, and precision-tier coverage, but
community-mapped OSM remains corroborating rather than first-party evidence.

The independent pilot cohort contains store coordinates from public official
SEFAZ-BA/PRODEB Preço da Hora API samples already captured by Price Index. It
is never passed to the OpenCEPGeo builder. The report contains only aggregate
metrics and an input checksum: source terms have not established permission to
redistribute the underlying fixture. The pilot covers Salvador/BA, so it is not
nationally representative.

`urban_address_proxy` means the OpenCEP row has a street or neighborhood.
`rural_or_general_address_proxy` means neither is present. These transparent
address-shape categories are not an official urban/rural territorial
classification; the report identifies that as an evidence gap.

Generate the deterministic JSON report and its human-readable summary:

```bash
opencepgeo quality report \
  --database out/opencepgeo.sqlite \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --official-holdout /private/validation/sefaz-ba-holdout.csv \
  --config config/enrichment-v1.json \
  --quality-config config/quality-v1.json \
  --output reports/quality-2026.2.1.json \
  --markdown reports/quality-2026.2.1.md
```

The report records artifact counts, coverage, unresolved rows, tier
distribution, validation sample counts, all available UF/address-class groups,
and nearest-rank p50/p90/p95 haversine errors. Any failed threshold produces a
non-zero command exit. Repeated runs over identical inputs are byte-identical.
