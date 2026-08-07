# Price Index integration contract

Price Index must consume an immutable, verified OpenCEPGeo release directory,
not call an OpenCEPGeo or third-party HTTP endpoint during user requests.

## Release contract

The RC2 package is described by `opencepgeo-release-manifest-v2`. It contains a
SQLite v4 database, canonical JSONL v4, deterministic CEP-sorted CSV v4, the
build manifest, quality report and summary, policies, source lock, audited
correction, notices, and `SHA256SUMS`.

The manifest binds the dataset/schema versions, record count, builder identity,
every packaged file, source-lock publication gate, lookup contract, and quality
attestation. Run the packaged verifier before import:

```bash
opencepgeo release verify /srv/import/opencepgeo-2026.2.1-rc2
```

The verifier proves the package is internally consistent. Because the IBGE,
OSM, and official validation evidence is intentionally not redistributed, it
validates their package-time hash attestation rather than rerunning quality.
The packager itself reruns quality from those exact inputs and rejects any
caller-supplied JSON or Markdown that differs from the canonical result.

## Import behavior

1. Receive a named immutable private release candidate.
2. Run `opencepgeo release verify` and require `status=verified`.
3. Independently enforce the recorded publication gate; RC2 remains blocked
   from public redistribution.
4. Load the CSV or SQLite rows into a PostgreSQL staging table.
5. Reject CEP duplicates, incompatible schema/version, coordinate violations,
   unknown precision tiers, or a material coverage regression.
6. Promote staging transactionally and retain the previous good dataset.

The request path then performs a primary-key local lookup only. There is no
external HTTP fallback and no cache that can outlive a dataset swap.

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
    "geo_evidence_radius_km": 0.0,
    "geo_evidence_count": 1,
    "source": "opencepgeo",
    "dataset_version": "2026.2.1-rc2",
}
```

`geo_evidence_radius_km` is the spread of retained evidence, not a calibrated
error radius. It must not be converted into a confidence score or search
radius.

## Nearby-store policy

Nearby-store distance ranking needs its own explicit consumer policy. Only
`observed_cep` and `osm_postcode` are eligible for direct distance ranking in
RC2, and the UI/API must retain the precision label. `observed_cep_prefix` and
`municipality` may support display, municipality/region filtering, or a prompt
for a more precise location, but must never be ranked as if they were the
customer's position.

An exact first-party store coordinate always outranks every CEP-derived point.
The quality report's same-CEP OSM cohort is a consistency proxy, not independent
nearby-store error calibration, and the official pilot covers BA only.
