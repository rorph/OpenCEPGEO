# OpenCEPGeo quality report quality-2026.2.1-rc2

Status: **PASS**

> Scope: the independent official evidence is a BA-only pilot. This report does not certify national official accuracy or calibrated nearby-store positional error.

## Artifact coverage

- Records: 1209314
- Located: 1209311 (99.999752%)
- Unresolved: 3
- UFs: 27/27
- Maximum serialized source categories: 20 bytes
- Evidence digest: maximum 71 bytes, invalid rows 0
- Tier distribution: `{"municipality": 1171666, "osm_postcode": 37645, "unresolved": 3}`

## OSM evidence eligibility

- Input observations: 446959
- Known-target observations: 440489
- Polygon-eligible observations: 445898
- Eligible observations: 407116
- Interior/boundary observations: 439428/0
- Outside target municipality: 1061 (0.240869%)
- Unknown CEP observations retained for missingness: 6470
- Exclusions by reason: `{"cep_group_radius_rejection": 12647, "outside_reference_distance_backstop": 32, "outside_target_municipality": 1061, "robust_spatial_outlier": 26103}`
- Method: ibge-2024-municipality-polygon-containment-v1
- Interpretation: OSM points outside the official target municipality polygon are excluded, then the estimator's reference-distance, robust-outlier, and CEP-radius rules select production-retained LOO evidence; the unseen cohort uses all polygon-contained evidence, and unknown CEPs remain to measure missingness

## Leave-observation-out same-CEP consistency

- Interpretation: same-CEP centroid consistency proxy; source-correlated mapping errors remain possible
- Evidence scope: official-polygon-contained and production-retained full CEP groups
- Training observations: 326194
- Held-out observations: 80922
- Held-out CEPs: 18351
- Evaluated observations: 79657
- Missing/prediction failures: 1265/0
- UFs: 27/27

| Group | Count | p50 km | p90 km | p95 km |
| --- | ---: | ---: | ---: | ---: |
| overall | 79657 | 0.121 | 0.726 | 1.412 |
| precision:observed_cep | 0 | None | None | None |
| precision:osm_postcode | 75856 | 0.111 | 0.525 | 0.818 |
| precision:observed_cep_prefix | 0 | None | None | None |
| precision:municipality | 3801 | 3.965 | 13.351 | 17.333 |

## CEP-group holdout for unseen-CEP fallback

- Interpretation: unseen-CEP fallback quality; every OSM observation for a held-out CEP is excluded from training
- Evidence scope: all official-polygon-contained observations; estimator training applies production filters internally
- Training observations: 352129
- Held-out observations: 93769
- Held-out CEPs: 7961
- Evaluated observations: 92409
- Missing/prediction failures: 1360/0
- UFs: 27/27

| Group | Count | p50 km | p90 km | p95 km |
| --- | ---: | ---: | ---: | ---: |
| overall | 92409 | 7.332 | 33.343 | 34.606 |
| precision:observed_cep | 0 | None | None | None |
| precision:osm_postcode | 0 | None | None | None |
| precision:observed_cep_prefix | 0 | None | None | None |
| precision:municipality | 92409 | 7.332 | 33.343 | 34.606 |

## Uncensored raw-OSM leave_observation_out diagnostic (not gated)

- Interpretation: diagnostic only, not gated: raw OSM evidence before official municipality polygon and full-group estimator eligibility filtering; this is consistency against community-mapped evidence, not positional accuracy
- Held-out observations: 88905
- Evaluated observations: 87634
- Overall p95: 6.443 km
- RR OSM-tier p95: 137.194 km

## Uncensored raw-OSM unseen_cep diagnostic (not gated)

- Interpretation: diagnostic only, not gated: raw OSM evidence before official municipality polygon and full-group estimator eligibility filtering; this is consistency against community-mapped evidence, not positional accuracy
- Held-out observations: 94005
- Evaluated observations: 92645
- Overall p95: 34.617 km
- RR OSM-tier p95: None km

## Purpose gates

- `nearby_store_same_cep_proxy` (leave_observation_out, ['osm_postcode']): UFs ['AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS', 'MT', 'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN', 'RO', 'RR', 'RS', 'SC', 'SE', 'SP', 'TO'], count 75856, p95 0.818 km
- `regional_fallback_unseen_cep` (unseen_cep, ['municipality']): UFs ['AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS', 'MT', 'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN', 'RO', 'RR', 'RS', 'SC', 'SE', 'SP', 'TO'], count 92409, p95 34.606 km
- `rr_municipality_coarse_address_exception` (unseen_cep, ['municipality']): UFs ['RR'], count 67, p95 130.418 km
- `rr_osm_postcode_same_cep` (leave_observation_out, ['osm_postcode']): UFs ['RR'], count 14, p95 1.141 km

## Independent official BA-only pilot

- Evidence source ID: `sefaz-ba-prodeb-preco-da-hora-offline-pilot-v1`
- Evidence artifact: `opencepgeo-quality-holdout-PIN-180.csv` (2670 bytes; private local input, not packaged)
- Scope: BA-only pilot; not nationally representative
- Evaluated observations: 41
- Missing/prediction failures: 0/0
- UFs: BA

| Group | Count | p50 km | p90 km | p95 km |
| --- | ---: | ---: | ---: | ---: |
| overall | 41 | 5.067 | 10.828 | 12.63 |
| precision:municipality | 31 | 7.503 | 12.594 | 12.737 |
| precision:observed_cep | 0 | None | None | None |
| precision:observed_cep_prefix | 0 | None | None | None |
| precision:osm_postcode | 10 | 0.301 | 1.053 | 1.325 |

## Evidence gaps

- The independent official pilot covers BA only and does not certify national accuracy.
- The leave-observation-out cohort measures same-CEP OSM consistency, not independent ground-truth accuracy.
- The unseen-CEP cohort measures fallback behavior using OSM as the reference and remains community-mapped evidence.
- The gated LOO cohort conditions on full-group estimator eligibility, which can introduce selection bias; gated unseen CEPs use all polygon-contained evidence, and both raw-OSM cohorts remain visible and uncensored.
- Municipality containment uses the independently locked official IBGE 2024 polygon dataset; polygons are still generalized cartographic boundaries.
- evidence_radius_km measures retained evidence spread and is not a calibrated positional-error bound.
- No approved production first-party corpus exists for observed_cep or observed_cep_prefix calibration.

## Gate checks

- **PASS** `artifact_build_policy`: actual `[]`, threshold `[]`
- **PASS** `osm_evidence.maximum_outside_target_municipality_fraction`: actual `0.00240869`, threshold `0.005`
- **PASS** `leave_observation_out.minimum_records`: actual `79657`, threshold `75000`
- **PASS** `leave_observation_out.maximum_missing_fraction`: actual `0.01563234`, threshold `0.02`
- **PASS** `leave_observation_out.maximum_prediction_failure_fraction`: actual `0.0`, threshold `0.02`
- **PASS** `leave_observation_out.minimum_ufs`: actual `27`, threshold `27`
- **PASS** `leave_observation_out.required_address_classes`: actual `['rural_or_general_address_proxy', 'urban_address_proxy']`, threshold `['rural_or_general_address_proxy', 'urban_address_proxy']`
- **PASS** `unseen_cep.minimum_records`: actual `92409`, threshold `80000`
- **PASS** `unseen_cep.maximum_missing_fraction`: actual `0.01450373`, threshold `0.02`
- **PASS** `unseen_cep.maximum_prediction_failure_fraction`: actual `0.0`, threshold `0.02`
- **PASS** `unseen_cep.minimum_ufs`: actual `27`, threshold `27`
- **PASS** `unseen_cep.required_address_classes`: actual `['rural_or_general_address_proxy', 'urban_address_proxy']`, threshold `['rural_or_general_address_proxy', 'urban_address_proxy']`
- **PASS** `per_uf.AC.minimum_samples`: actual `76`, threshold `50`
- **PASS** `per_uf.AC.maximum_p95_km`: actual `14.273`, threshold `125.0`
- **PASS** `per_uf.AL.minimum_samples`: actual `57`, threshold `50`
- **PASS** `per_uf.AL.maximum_p95_km`: actual `7.572`, threshold `125.0`
- **PASS** `per_uf.AM.minimum_samples`: actual `195`, threshold `50`
- **PASS** `per_uf.AM.maximum_p95_km`: actual `12.511`, threshold `125.0`
- **PASS** `per_uf.AP.minimum_samples`: actual `124`, threshold `50`
- **PASS** `per_uf.AP.maximum_p95_km`: actual `67.82`, threshold `125.0`
- **PASS** `per_uf.BA.minimum_samples`: actual `10766`, threshold `50`
- **PASS** `per_uf.BA.maximum_p95_km`: actual `35.536`, threshold `125.0`
- **PASS** `per_uf.CE.minimum_samples`: actual `36446`, threshold `50`
- **PASS** `per_uf.CE.maximum_p95_km`: actual `13.425`, threshold `125.0`
- **PASS** `per_uf.DF.minimum_samples`: actual `178`, threshold `50`
- **PASS** `per_uf.DF.maximum_p95_km`: actual `31.036`, threshold `125.0`
- **PASS** `per_uf.ES.minimum_samples`: actual `1514`, threshold `50`
- **PASS** `per_uf.ES.maximum_p95_km`: actual `37.316`, threshold `125.0`
- **PASS** `per_uf.GO.minimum_samples`: actual `175`, threshold `50`
- **PASS** `per_uf.GO.maximum_p95_km`: actual `11.625`, threshold `125.0`
- **PASS** `per_uf.MA.minimum_samples`: actual `1934`, threshold `50`
- **PASS** `per_uf.MA.maximum_p95_km`: actual `36.869`, threshold `125.0`
- **PASS** `per_uf.MG.minimum_samples`: actual `3092`, threshold `50`
- **PASS** `per_uf.MG.maximum_p95_km`: actual `11.974`, threshold `125.0`
- **PASS** `per_uf.MS.minimum_samples`: actual `191`, threshold `50`
- **PASS** `per_uf.MS.maximum_p95_km`: actual `16.112`, threshold `125.0`
- **PASS** `per_uf.MT.minimum_samples`: actual `133`, threshold `50`
- **PASS** `per_uf.MT.maximum_p95_km`: actual `24.5`, threshold `125.0`
- **PASS** `per_uf.PA.minimum_samples`: actual `2525`, threshold `50`
- **PASS** `per_uf.PA.maximum_p95_km`: actual `17.233`, threshold `125.0`
- **PASS** `per_uf.PB.minimum_samples`: actual `99`, threshold `50`
- **PASS** `per_uf.PB.maximum_p95_km`: actual `9.949`, threshold `125.0`
- **PASS** `per_uf.PE.minimum_samples`: actual `1033`, threshold `50`
- **PASS** `per_uf.PE.maximum_p95_km`: actual `9.432`, threshold `125.0`
- **PASS** `per_uf.PI.minimum_samples`: actual `189`, threshold `50`
- **PASS** `per_uf.PI.maximum_p95_km`: actual `8.034`, threshold `125.0`
- **PASS** `per_uf.PR.minimum_samples`: actual `1238`, threshold `50`
- **PASS** `per_uf.PR.maximum_p95_km`: actual `16.791`, threshold `125.0`
- **PASS** `per_uf.RJ.minimum_samples`: actual `1039`, threshold `50`
- **PASS** `per_uf.RJ.maximum_p95_km`: actual `19.357`, threshold `125.0`
- **PASS** `per_uf.RN.minimum_samples`: actual `215`, threshold `50`
- **PASS** `per_uf.RN.maximum_p95_km`: actual `7.711`, threshold `125.0`
- **PASS** `per_uf.RO.minimum_samples`: actual `129`, threshold `50`
- **PASS** `per_uf.RO.maximum_p95_km`: actual `120.824`, threshold `125.0`
- **PASS** `per_uf.RS.minimum_samples`: actual `5329`, threshold `50`
- **PASS** `per_uf.RS.maximum_p95_km`: actual `8.051`, threshold `125.0`
- **PASS** `per_uf.SC.minimum_samples`: actual `1480`, threshold `50`
- **PASS** `per_uf.SC.maximum_p95_km`: actual `7.083`, threshold `125.0`
- **PASS** `per_uf.SE.minimum_samples`: actual `125`, threshold `50`
- **PASS** `per_uf.SE.maximum_p95_km`: actual `16.096`, threshold `125.0`
- **PASS** `per_uf.SP.minimum_samples`: actual `23889`, threshold `50`
- **PASS** `per_uf.SP.maximum_p95_km`: actual `13.205`, threshold `125.0`
- **PASS** `per_uf.TO.minimum_samples`: actual `171`, threshold `50`
- **PASS** `per_uf.TO.maximum_p95_km`: actual `42.68`, threshold `125.0`
- **PASS** `purpose.nearby_store_same_cep_proxy.minimum_records`: actual `75856`, threshold `75000`
- **PASS** `purpose.nearby_store_same_cep_proxy.maximum_p95_km`: actual `0.818`, threshold `2.0`
- **PASS** `purpose.regional_fallback_unseen_cep.minimum_records`: actual `92409`, threshold `75000`
- **PASS** `purpose.regional_fallback_unseen_cep.maximum_p95_km`: actual `34.606`, threshold `50.0`
- **PASS** `purpose.rr_municipality_coarse_address_exception.minimum_records`: actual `67`, threshold `50`
- **PASS** `purpose.rr_municipality_coarse_address_exception.maximum_p95_km`: actual `130.418`, threshold `150.0`
- **PASS** `purpose.rr_osm_postcode_same_cep.minimum_records`: actual `14`, threshold `10`
- **PASS** `purpose.rr_osm_postcode_same_cep.maximum_p95_km`: actual `1.141`, threshold `2.0`
- **PASS** `official_pilot.minimum_records`: actual `41`, threshold `35`
- **PASS** `official_pilot.maximum_missing_fraction`: actual `0.0`, threshold `0.0`
- **PASS** `official_pilot.maximum_prediction_failure_fraction`: actual `0.0`, threshold `0.0`
- **PASS** `official_pilot.expected_ufs`: actual `['BA']`, threshold `['BA']`
- **PASS** `official_pilot.maximum_p95_km`: actual `12.63`, threshold `20.0`
