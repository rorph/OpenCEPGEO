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

The commands never accept changed bytes. A missing input fails verification;
an existing file with a different size or digest is left untouched and fails.
A download is written to a temporary sibling file, validated, and only then
renamed to its locked filename.

## Redistribution decision

- The OpenCEPGeo implementation is MIT licensed. This says nothing about the
  database rights in downloaded or generated data.
- OpenCEP publishes a release archive and applies MIT to its repository, but
  it does not state a separate database license or disclose a reproducible
  extraction chain from the Correios DNE. Correios markets DNE as a commercial
  database. Public redistribution of OpenCEP input bytes or derived CEP rows
  is therefore **blocked pending written rights review**.
- IBGE describes the download directory as public and must be attributed as
  `Censo Demografico 2022: Localidades do Brasil`, including its edition and
  source URL. This does not clear the OpenCEP-derived artifact gate.
- First-party observations require their own documented collection authority
  and sharing scope. The repository includes only a synthetic example.
- The optional Geofabrik/OpenStreetMap snapshot is ODbL 1.0 data. Any release
  using it needs OpenStreetMap contributor attribution, ODbL database notices,
  and a share-alike assessment for the derived database.
- Public Nominatim is prohibited as a bulk source. Enrichment may use only a
  pinned local extract and local tooling.

The build can be reproduced privately while this gate is closed. No generated
database, manifest, or source archive may be published until the OpenCEP/DNE
question and any selected OSM obligations are explicitly cleared.
