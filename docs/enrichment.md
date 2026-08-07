# Offline enrichment tiers

The release build resolves each CEP in this fixed order:

1. `observed_cep` — robust median of checksum-locked first-party points for the
   exact CEP.
2. `osm_postcode` — robust median of local OSM nodes carrying an explicit
   `addr:postcode` or `postal_code` tag for the exact CEP.
3. `observed_cep_prefix` — at least three first-party points with the same
   five-digit prefix **and** the same seven-digit IBGE municipality.
4. `municipality` — the official IBGE `Cidade` point, with uncertainty measured
   as the maximum distance to all published IBGE Localidades points in that
   municipality.
5. `unresolved` — no coordinates are invented.

Every located result carries `precision`, `method`, `evidence_count`,
`uncertainty_km`, sorted source IDs, and `dataset_version`. Robust tiers remove
distance outliers using a median/MAD rule and then reject the entire tier when
the remaining spread exceeds its configured maximum.

All thresholds and the tier order live in `config/enrichment-v1.json`. The
build manifest embeds both its content and SHA-256. Production builds must use
the versioned file; API callers may use inline thresholds only for fixtures.

## Local OSM extraction

The extractor reads the pinned Geofabrik PBF directly with Python's standard
library. It streams compressed PBF blocks and writes a deterministic CSV plus a
checksum/provenance sidecar:

```bash
opencepgeo osm extract \
  --pbf data/locked/geofabrik-brazil-260806.osm.pbf \
  --source-lock sources/lock.json \
  --output data/derived/osm-postcodes.csv
```

Only point objects with an explicit, valid Brazilian postcode are accepted.
Street-only evidence is deliberately rejected: without a local, unambiguous
street-to-municipality join it is not safe enough to locate a CEP. Ways and
relations are not used. The extractor and build perform no network calls;
source fetching is a separate pre-build step.

Use the evidence in a build:

```bash
opencepgeo build \
  --opencep data/locked/opencep-2.0.1-v1.zip \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --source-lock sources/lock.json \
  --config config/enrichment-v1.json \
  --output out/opencepgeo.sqlite
```

When a checksum lock is used, the build requires the OSM evidence sidecar and
verifies that the evidence bytes and source-lock SHA-256 still match. OSM use
does not clear the release gate: attribution and ODbL obligations remain in the
manifest, and OpenCEP/DNE rights must also be cleared before publication.
