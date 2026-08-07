# Private release-candidate contract

OpenCEPGeo is packaged as local files, not a service. Lookup, packaging, and
release verification use only local inputs; none performs a runtime third-party
API call.

Package only after generating a quality report from the exact build inputs:

```bash
opencepgeo release package \
  --database out/gated.sqlite \
  --normalized out/gated.jsonl \
  --build-manifest out/gated.manifest.json \
  --quality-report reports/quality-2026.2.1-rc2.json \
  --quality-markdown reports/quality-2026.2.1-rc2.md \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --municipality-boundaries data/locked/BR_Municipios_2024.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --official-holdout /private/validation/sefaz-ba-holdout.csv \
  --official-holdout-id sefaz-ba-prodeb-preco-da-hora-offline-pilot-v1 \
  --source-lock sources/lock.json \
  --enrichment-config config/enrichment-v1.json \
  --quality-policy config/quality-v1.json \
  --corrections sources/opencep-2.0.1-corrections.json \
  --notice NOTICE.md \
  --output out/release/opencepgeo-2026.2.1-rc2
```

Packaging does not trust a caller-authored `PASS`. It recomputes quality from
the exact SQLite/build-manifest/IBGE/municipality-boundary/OSM/official/config/policy inputs, requires
the supplied JSON to be canonical and identical, regenerates the Markdown, and
requires the builder identity recorded in SQLite and the build manifest to
match the current package source tree.

The output directory is created atomically and must not already exist. It
contains the exact SQLite v4 and JSONL v4 bytes, deterministic CEP-sorted CSV
v4, build and release manifests, JSON/Markdown quality reports, source lock,
versioned policies, audited correction, NOTICE, and sorted `SHA256SUMS`.
Repeating the command with identical inputs produces identical bytes.

Verify every packaged file and internal contract:

```bash
opencepgeo release verify out/release/opencepgeo-2026.2.1-rc2
```

Verification rejects missing/extra/non-file entries, malformed or mismatched
checksums, incompatible manifests/schema, corrupt SQLite, invalid row
contracts, version/count regressions, CSV/JSONL rows that differ semantically
from SQLite, malformed/incomplete quality checks, quality Markdown drift,
builder/source-lock/config mismatches, missing attribution, or an unblocked
publication status.

The validation evidence files are intentionally not redistributed. Therefore
the installed verifier validates their package-time hashes and cohort-split
attestation but cannot rerun quality from the package alone. This limitation is
explicitly recorded as `attestation-only-evidence-not-packaged`.

The row contract never lists individual evidence IDs. `geo_source` contains at
most 16 source categories (2 KiB serialized maximum), while `evidence_count`
records cardinality and `evidence_digest` identifies the retained points.
`evidence_radius_km` measures evidence spread and is not a calibrated error.

The current source lock blocks redistribution pending OpenCEP/DNE rights
review, and OSM-derived rows require ODbL compliance. Packaging does not clear
those gates, upload files, push source, create a release, or deploy anything.
