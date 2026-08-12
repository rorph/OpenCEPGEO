# Source provenance and publication gate

`sources/lock.json` is the authoritative input boundary for a dataset build.
Every selected input has a stable version, expected byte count, SHA-256,
retrieval metadata, attribution, and a recorded rights status. The lock does
not grant rights that the upstream publisher has not granted.

Run the required-source acquisition and verification steps from the repository
root:

```bash
opencepgeo sources fetch --lock sources/lock.json --input-dir data/locked
opencepgeo sources verify --lock sources/lock.json --input-dir data/locked
```

Optional inputs are never selected implicitly. Select one by ID only when its
terms and operational cost are acceptable:

```bash
opencepgeo sources fetch \
  --lock sources/lock.json \
  --input-dir data/locked \
  --source first-party-observations-example
```

A production OSM build must opt into both locked geospatial inputs:

```bash
opencepgeo sources fetch \
  --lock sources/lock.json \
  --input-dir data/locked \
  --source ibge-municipios-2024 \
  --source geofabrik-brazil-260806
```

The commands never accept changed bytes. A missing input fails verification;
an existing file with a different size or digest is left untouched and fails.
A download is written to a temporary sibling file, validated, and only then
renamed to its locked filename. HTTPS reads are bounded by the locked byte
count and reject inconsistent `Content-Length`, early EOF, and trailing bytes.
Every source, including repository-resident corrections, must record non-empty
retrieval, attribution, license-status, and terms-status metadata.

A Correios DNEC refresh is an offline, independently reviewable provenance
step. Promotion requires the refresh manifest and its exact canonical JSONL v4
artifact; the refresh candidate SQLite is not trusted or reused. The resulting
build manifest retains the current OpenCEPGeo lineage, the Correios snapshot
identities/publication marker, and all supporting source-lock/configuration
digests. Correios attribution and rights review are additive to the existing
OpenCEP, IBGE, and OpenStreetMap obligations. A technically valid refresh does
not clear the publication gate.

## Redistribution decision

### Private internal self-hosting decision (2026-08-08)

[OpenCEP's repository](https://github.com/SeuAliado/OpenCEP) is MIT licensed and
its project documentation explicitly describes the corpus and application as
100% open source and suitable for self-hosting. That upstream evidence supports
this specific private internal OpenCEPGeo service deployment and private
reproduction of its checksum-locked RC2 artifact. The decision does not alter
the immutable historical inputs in `sources/lock.json`, does not authorize a
public dataset release, and does not remove any OpenCEP, IBGE, or OpenStreetMap
attribution.

Public redistribution remains a separate gate for the combined generated
artifact: ordinary release review, selected OSM ODbL attribution/share-alike
assessment, and IBGE provenance and attribution remain mandatory.

- The OpenCEPGeo implementation is MIT licensed. This says nothing about the
  database rights in downloaded or generated data.
- OpenCEP publishes its release archive in the MIT-licensed repository and
  documents open-source self-hosting. Preserve OpenCEP / SeuAliado attribution
  in internal deployments and any reviewed release.
- Correios DNEC snapshots used for offline refresh remain Correios source data.
  Preserve Correios attribution and the exact captured publication/provenance
  record; review redistribution rights for the refreshed combined database.
- IBGE describes the download directory as public and must be attributed as
  `Censo Demografico 2022: Localidades do Brasil`, including its edition and
  source URL. The separately locked `Malha Municipal 2024` polygon archive is
  used only for OSM evidence containment and requires the same provenance and
  attribution discipline for the combined generated artifact.
- First-party observations require their own documented collection authority
  and sharing scope. Production inputs must include a stable source-owned
  `evidence_id` for every row; reused identities with different data fail
  closed. The repository includes only a synthetic example.
- The optional Geofabrik/OpenStreetMap snapshot is ODbL 1.0 data. Any release
  using it needs OpenStreetMap contributor attribution, ODbL database notices,
  and a share-alike assessment for the derived database.
- Public Nominatim is prohibited as a bulk source. Enrichment may use only a
  pinned local extract and local tooling.

Private internal reproduction and self-hosting are cleared by the dated
decision above. Publishing a generated database, manifest, or source archive
requires ordinary release review and completion of all selected OSM ODbL and
IBGE attribution/compliance obligations.
