# Deterministic build contract

A release-candidate build consumes the filenames and bytes selected by
`sources/lock.json` and writes three artifacts:

1. `opencepgeo.sqlite` is the indexed offline lookup database.
2. `opencepgeo.jsonl` is the canonical normalized export. It contains one
   compact JSON object per line, sorted by the eight-digit `cep`. Its SHA-256
   is the reproducibility identity across supported runtimes.
3. `opencepgeo.manifest.json` binds the source-lock digest, selected source
   metadata, build configuration, schema version, statistics, and artifact
   checksums.

The builder streams OpenCEP JSON records into SQLite. It holds municipality
points and optional first-party observations in memory, but never materializes
the OpenCEP corpus as a Python list. Invalid/missing CEP, IBGE, city, or UF
fields fail the build. Duplicate CEPs fail the primary-key insertion and no
target artifact is promoted from its temporary path.

The locked OpenCEP 2.0.1 archive has one documented payload/member mismatch.
`sources/opencep-2.0.1-corrections.json` corrects that field only when the
archive member name, original value, and raw member SHA-256 all match. Any new
or changed anomaly, duplicate, or unused correction fails the build.

```bash
opencepgeo build \
  --opencep data/locked/opencep-2.0.1-v1.zip \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --source-lock sources/lock.json \
  --output out/opencepgeo.sqlite
```

The default sibling paths are `out/opencepgeo.jsonl` and
`out/opencepgeo.manifest.json`. Existing targets are not replaced unless
`--force` is explicit.

The reported statistics are:

- `input_records` and `unique_ceps`, which must be equal;
- `ibge_joined` and `ibge_join_rate` against official municipality points;
- `located` and `unresolved` rows after the configured estimation tiers;
- SQLite and normalized-export SHA-256 values.

Manifests deliberately omit a wall-clock build timestamp so identical inputs
and configuration produce identical manifest content when artifact filenames
are also identical.
