# Deterministic build contract

A release-candidate build consumes the filenames and bytes selected by
`sources/lock.json` and writes three artifacts:

1. `opencepgeo.sqlite` is the indexed offline lookup database.
2. `opencepgeo.jsonl` is the canonical normalized export. It contains one
   compact JSON object per line, sorted by the eight-digit `cep`. Its SHA-256
   is the reproducibility identity across supported runtimes.
3. `opencepgeo.manifest.json` (`opencepgeo-build-manifest-v2`) binds the
   source-lock digest, selected source metadata, deterministic OpenCEP/IBGE
   input records (including the official municipality boundary archive when
   OSM is selected), configuration, builder package/version/source-tree digest,
   SQLite v4 schema, statistics, and artifact checksums.

The builder streams OpenCEP JSON records into SQLite. It holds municipality
points and optional first-party observations in memory, but never materializes
the OpenCEP corpus as a Python list. Invalid/missing CEP, IBGE, city, or UF
fields fail the build. Duplicate CEPs fail the primary-key insertion and no
target artifact is promoted from its temporary path.

`build-from-normalized` is the separate promotion seam for a Correios refresh
candidate. It accepts only a regular, non-symlink canonical JSONL v4 file whose
basename, size, SHA-256, row version, and count are bound by an
`opencepgeo-correios-refresh-manifest-v1` in the offline-candidate state. Lines
are bounded, UTF-8 JSON is required to be compact/canonical, CEPs must be
strictly increasing and unique, and the complete v4 row/geo contract is
validated before use. Non-null geography is inserted unchanged. A null point
may only become a coarse, nearby-ineligible `municipality` point derived from
the pinned IBGE Localidades input. The ordinary municipality reference and the
narrow Fernando de Noronha administrative-locality reference have distinct
methods, sources, evidence counts, radii, and digests. The candidate SQLite
emitted by the refresh tool is deliberately not an input.

The normalized build also verifies the complete inherited release and
contract, then hash-binds its current source lock, IBGE locality input,
municipality boundaries, OSM evidence and companion manifest, enrichment
configuration, and quality policy. The source lock remains labeled with the
inherited release; it is not relabeled as the new candidate. After insertion
and the ordinary quality gate, the builder streams the generated normalized
artifact and compares every row semantically with CEP-ordered SQLite, then
runs `integrity_check`. SQLite, generated JSONL, and the build manifest are
published as one rollback-safe group from a private staging directory; the
PIN-207 candidate JSONL remains immutable external evidence.

Every consumed refresh, inherited-release, source, evidence, and configuration
file is copied through a regular-file descriptor into that private directory
before parsing or derivation; only those verified snapshots are subsequently
used. The refresh quality report must have the exact PIN-207 classification,
precision, and geography-action keys. Candidate precision counts and diff
classification/action counts are independently streamed and must equal the
report, including valid classification/action pairings. The emitted source
lineage preserves every inherited RC2 source record and its OSM/correction
configuration, then adds explicit Correios snapshot provenance.

The inherited build-time normalized basename may differ from its packaged
release basename; their bytes, SHA-256, and JSONL format must agree, while the
packaged name is independently bound by the release manifest and import
contract.
The same packaging rule applies to the source lock, enrichment configuration,
and quality policy: repository basenames may differ from package basenames,
but bytes, SHA-256, and each document's semantic format must agree. The import
contract's complete approved file map must equal the inherited release
manifest, and its contract-bound `SHA256SUMS` auxiliary is independently
snapshotted and verified.

Provenance size is constant with respect to sample count: rows store at most 16
source categories plus a SHA-256 digest of the retained evidence. The manifest
stores the full evidence-artifact checksum. Schema constraints and the quality
gate cap serialized categories at 2 KiB and require a valid fixed-length digest.
Production first-party evidence must supply a stable source-owned
`evidence_id`; exact duplicate identities are deduplicated and conflicting
reuse fails the build.

The locked OpenCEP 2.0.1 archive has one documented payload/member mismatch.
`sources/opencep-2.0.1-corrections.json` corrects that field only when the
archive member name, original value, and raw member SHA-256 all match. Any new
or changed anomaly, duplicate, or unused correction fails the build.

```bash
opencepgeo build \
  --opencep data/locked/opencep-2.0.1-v1.zip \
  --ibge data/locked/ibge-localidades-2022-gpkg.zip \
  --municipality-boundaries data/locked/BR_Municipios_2024.zip \
  --osm-observations data/derived/osm-postcodes.csv \
  --source-lock sources/lock.json \
  --config config/enrichment-v1.json \
  --quality-config config/quality-v1.json \
  --output out/opencepgeo.sqlite \
  --export out/opencepgeo.jsonl \
  --manifest out/opencepgeo.manifest.json
```

For the normalized seam, use `opencepgeo build-from-normalized` as documented
in the README. Its build manifest uses the same v2 format and additionally
records `inputs.normalized_refresh`, including distinct candidate and generated
artifact identities, all PIN-207 audit artifacts, inherited OpenCEPGeo release
identities, Correios snapshot provenance, refresh status, classification
counts, and the exact geography-derivation counts. The SQLite metadata repeats
the generated/candidate normalized, refresh audit, source-lock, configuration,
and policy digests.

The default sibling paths are `out/opencepgeo.jsonl` and
`out/opencepgeo.manifest.json`. Existing targets are not replaced unless
`--force` is explicit. Without `--force`, publication uses atomic
create-if-absent links, so a target created during a long build is never
overwritten. Forced replacement keeps prior artifacts in a separate private
recovery directory until all three targets are durable; an incomplete rollback
reports and preserves that directory for manual recovery.
All publication operations are relative to one open output-directory
descriptor and serialized by a directory-local lock. If a symlinked parent is
retargeted, or a target appears concurrently after backup, the build fails
without clobbering that target and preserves any displaced backup.

The reported statistics are:

- `input_records` and `unique_ceps`, which must be equal;
- `ibge_joined` and `ibge_join_rate` against official municipality points;
- `located` and `unresolved` rows after the configured estimation tiers;
- SQLite and normalized-export SHA-256 values.

Before promotion, the builder runs SQLite `integrity_check`. SQLite v4 also
enforces CEP/prefix/IBGE shape and relation, the all-null/all-present geo
contract, positive evidence counts, non-negative evidence radii, and bounded
provenance. Lookup refuses stale schema versions with an explicit error.

Manifests deliberately omit a wall-clock build timestamp so identical inputs
and configuration produce identical manifest content when artifact filenames
are also identical.
