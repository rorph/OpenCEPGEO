# Offline enrichment tiers

The release build resolves each CEP in this fixed order:

1. `observed_cep` — robust median of checksum-locked first-party points for the
   exact CEP.
2. `osm_postcode` — robust median of local OSM nodes carrying an explicit
   `addr:postcode` or `postal_code` tag for the exact CEP. Before estimation,
   each candidate must be contained by the independently locked official IBGE
   polygon for the OpenCEP target municipality. A separate, looser reference-
   point distance remains a safety backstop for very large polygons.
3. `observed_cep_prefix` — at least three first-party points with the same
   five-digit prefix **and** the same seven-digit IBGE municipality.
4. `municipality` — the official IBGE `Cidade` point, with evidence spread
   measured as the maximum distance to all published IBGE Localidades points
   in that municipality.
5. `unresolved` — no coordinates are invented.

Every located result carries `precision`, `method`, `evidence_count`,
`evidence_radius_km`, sorted bounded source categories, a fixed SHA-256 evidence
digest, and `dataset_version`. Individual evidence IDs remain in the
checksum-locked evidence input instead of being repeated in every output row.
`evidence_radius_km` describes retained sample spread; it is not a calibrated
position-error estimate. Robust tiers remove distance outliers using a
median/MAD rule and then reject the entire tier when the remaining spread
exceeds its configured maximum.

IBGE Localidades contains reference points, while IBGE Malha Municipal 2024
supplies the independent containment polygons. The standard-library reader
streams the locked SHP/DBF pair directly from its ZIP and performs exact
point-in-polygon checks; no GIS package or network service is required.

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
  --municipality-boundaries data/locked/BR_Municipios_2024.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --source-lock sources/lock.json \
  --config config/enrichment-v1.json \
  --output out/opencepgeo.sqlite
```

When a checksum lock is used, the build requires the OSM evidence sidecar and
verifies that the evidence bytes and source-lock SHA-256 still match. OSM use
does not clear the release gate: attribution and ODbL obligations remain in the
manifest, and OpenCEP/DNE rights must also be cleared before publication.

The PBF parser rejects unsupported compression combinations, overlarge
compressed/raw blocks, declared-size expansion, incomplete zlib streams, and
trailing compressed data before accepting evidence.
