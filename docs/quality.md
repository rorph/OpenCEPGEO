# Quality calibration and regression gate

`config/quality-v1.json` is the versioned RC2 policy
(`opencepgeo-quality-policy-v2`). A build fails before promoting output when it
violates Brazil bounds, count/coverage/unresolved thresholds, UF coverage,
precision labels, evidence/provenance bounds, or IBGE consistency rules.
SQLite constraints and the input/unique count check independently reject
malformed and duplicate rows.

The deterministic quality report is `opencepgeo-quality-report-v2`. It binds
the exact SQLite database, build manifest, IBGE file, OSM evidence, official
pilot, official municipality polygon archive, enrichment config, and quality
policy by SHA-256. The build manifest in turn binds the builder identity and
exact build inputs. Missing fields, unknown or duplicate checks, inconsistent
pass/fail status, or hash mismatches fail closed.

## Validation cohorts

Two deterministic SHA-256/modulus cohorts answer different questions:

- `leave_observation_out` splits by stable evidence identity. A held-out OSM
  node is excluded from the estimator, while other observations for its CEP may
  remain. This is a same-CEP centroid consistency proxy only; source-correlated
  mapping errors can remain.
- `unseen_cep` splits by CEP. Every OSM observation for a held-out CEP is
  excluded from training, so it measures fallback behavior for genuinely
  unseen CEP groups. It still uses community-mapped OSM as the reference, not
  official ground truth.

Both gated cohorts exclude OSM points outside the official polygon for the
OpenCEP target municipality. The report gates and discloses that exclusion
fraction. The gated leave-observation-out cohort applies the same full-CEP-group
reference-distance, robust-outlier, and 5 km radius rules as the production
estimator; its 75,000-record floor matches the nearby-purpose floor.
Conditioning on the full group can introduce selection bias. The gated unseen
cohort therefore remains conservative and uses every polygon-contained
observation while estimator training still applies production filtering. The
report also preserves **uncensored**, non-gated raw-OSM
leave-observation-out and unseen-CEP diagnostics. These are consistency proxies
against community-mapped evidence, not positional-accuracy measurements. Per-UF
sample and p95 limits are explicit per state. Twenty-six UFs retain the 125 km
unseen-CEP ceiling. RR is split into two explicit gates: its OSM tier retains
the strict 2 km same-CEP proxy ceiling, while only municipality-tier fallback
has a documented 150 km coarse-address operational ceiling because RR's
municipalities are unusually large. This is not a precision claim, and
municipality estimates remain forbidden for nearby-store ranking.

Each cohort has explicit minimum evaluated records, maximum missing and
prediction-failure fractions, minimum UF coverage, and required address-shape
classes. The unseen-CEP cohort additionally has per-UF minimum sample and p95
limits. Purpose-specific gates isolate the same-CEP OSM proxy from regional
municipality fallback behavior.

`urban_address_proxy` means the OpenCEP row has a street or neighborhood;
`rural_or_general_address_proxy` means neither is present. These are transparent
address-shape categories, not an official urban/rural classification.

## Independent official pilot

The official holdout contains previously captured SEFAZ-BA/PRODEB Preço da
Hora locations and is never passed to the builder. Its minimum sample,
missing/failure fractions, expected UF set, and p95 limit are separately gated.
It covers BA only, is not nationally representative, and does not establish a
calibrated nearby-store error bound. Only aggregate metrics and the input's
stable source ID, basename, byte count, and hash are packaged; the CLI path is a
caller-supplied private local file and redistribution rights are not established.

Generate the canonical report and summary:

```bash
opencepgeo quality report \
  --database out/opencepgeo.sqlite \
  --build-manifest out/opencepgeo.manifest.json \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --municipality-boundaries data/locked/BR_Municipios_2024.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --official-holdout /private/validation/sefaz-ba-holdout.csv \
  --official-holdout-id sefaz-ba-prodeb-preco-da-hora-offline-pilot-v1 \
  --config config/enrichment-v1.json \
  --quality-config config/quality-v1.json \
  --output reports/quality-2026.2.1-rc2.json \
  --markdown reports/quality-2026.2.1-rc2.md
```

The CLI exits nonzero for a failing report. Identical inputs produce identical
canonical JSON and Markdown. `evidence_radius_km` is retained-evidence spread,
not a calibrated positional-error or confidence interval.
