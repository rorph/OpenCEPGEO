# Price Index integration contract

Price Index must consume an immutable OpenCEPGeo artifact, not call an
OpenCEPGeo HTTP endpoint during user requests.

## Release contract

The MVP release artifact is SQLite. A later release job should additionally
publish sorted CSV/Parquet plus a manifest containing:

```json
{
  "schema_version": 1,
  "dataset_version": "2026.08.1",
  "record_count": 1209314,
  "sha256": "...",
  "sources": [
    {"name": "OpenCEP", "version": "2.0.1", "sha256": "..."},
    {"name": "IBGE Localidades", "version": "2022", "sha256": "..."}
  ]
}
```

## Import behavior

1. Download a named immutable release.
2. Verify its checksum, schema version, row count, CEP uniqueness, coordinate
   bounds, and source metadata.
3. Load a PostgreSQL staging table.
4. Reject material coverage regressions or invalid precision values.
5. Promote staging transactionally and retain the previous good dataset.

The request path then performs a primary-key PostgreSQL lookup only. There is
no external HTTP fallback and no Redis cache that can outlive a dataset swap.

## Resolver shape

The adapter should preserve Price Index's existing keys and add versioned
quality metadata:

```python
{
    "cep": "01001000",
    "uf": "SP",
    "city": "São Paulo",
    "neighborhood": "Sé",
    "street": "Praça da Sé",
    "ibge": "3550308",
    "lat": -23.5505,
    "lon": -46.6333,
    "geo_precision": "observed_cep",
    "geo_uncertainty_km": 0.0,
    "geo_evidence_count": 1,
    "source": "opencepgeo",
    "dataset_version": "2026.08.1",
}
```

For nearby-store searches, rank evidence rather than accepting any non-null
coordinate:

1. Exact observed store CEP centroid.
2. OpenCEPGeo `observed_cep`.
3. Safe store/OpenCEPGeo prefix centroid.
4. OpenCEPGeo municipality point.
5. No point; fail closed.

A municipality point must never outrank an exact first-party store observation.

