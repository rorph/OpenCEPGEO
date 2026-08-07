# OpenCEPGeo quality report quality-2026.2.1

Status: **PASS**

## Artifact coverage

- Records: 1209314
- Located: 1209312 (99.999835%)
- Unresolved: 2
- UFs: 27/27
- Tier distribution: `{"municipality": 1171540, "osm_postcode": 37772, "unresolved": 2}`

## Leakage-controlled holdout

- Training observations: 357665
- Held-out observations: 89294
- Evaluated observations: 87998
- UFs: 27/27

| Group | Count | p50 km | p90 km | p95 km |
| --- | ---: | ---: | ---: | ---: |
| overall | 87998 | 0.147 | 1.298 | 6.391 |
| precision:observed_cep | 0 | None | None | None |
| precision:osm_postcode | 80841 | 0.126 | 0.682 | 1.148 |
| precision:observed_cep_prefix | 0 | None | None | None |
| precision:municipality | 7157 | 4.688 | 24.038 | 32.404 |

## Independent official pilot

- Evaluated observations: 41
- UFs: BA

| Group | Count | p50 km | p90 km | p95 km |
| --- | ---: | ---: | ---: | ---: |
| overall | 41 | 5.067 | 10.828 | 12.63 |
| precision:municipality | 31 | 7.503 | 12.594 | 12.737 |
| precision:observed_cep | 0 | None | None | None |
| precision:observed_cep_prefix | 0 | None | None | None |
| precision:osm_postcode | 10 | 0.301 | 1.053 | 1.325 |

## Evidence gaps

- The official pilot holdout covers Salvador/BA only; it is not nationally representative.
- OSM explicit-postcode nodes are independent of OpenCEP and IBGE, but remain community-mapped evidence.
- Urban/rural is an address-metadata proxy, not an official territorial classification.
- No observed_cep or observed_cep_prefix error sample exists because no approved production first-party corpus was used to build this release.

## Gate checks

- **PASS** `artifact_build_policy`: actual `[]`, threshold `[]`
- **PASS** `holdout_record_count`: actual `87998`, threshold `80000`
- **PASS** `official_holdout_record_count`: actual `41`, threshold `35`
- **PASS** `holdout_uf_coverage`: actual `27`, threshold `27`
- **PASS** `holdout_address_classes`: actual `['rural_or_general_address_proxy', 'urban_address_proxy']`, threshold `['rural_or_general_address_proxy', 'urban_address_proxy']`
- **PASS** `overall_p95`: actual `6.391`, threshold `10.0`
- **PASS** `osm_postcode_p95`: actual `1.148`, threshold `2.0`
- **PASS** `municipality_p95`: actual `32.404`, threshold `50.0`
- **PASS** `official_overall_p95`: actual `12.63`, threshold `20.0`
