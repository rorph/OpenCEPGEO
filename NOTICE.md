# Data provenance and licensing notice

OpenCEPGeo's source code is MIT licensed. Input datasets and generated database
artifacts may be governed by different terms.

Private internal deployment decision, 2026-08-08: OpenCEP's MIT repository and
its 100%-open-source/self-hosting documentation support private internal
self-hosting of this service and its checksum-locked RC2 artifact. This decision
does not authorize public redistribution and does not change the immutable RC2
source lock.

- OpenCEP publishes a downloadable CEP corpus from its GitHub releases and
  labels its repository MIT, with documentation for open-source self-hosting.
  Preserve OpenCEP / SeuAliado attribution.
- IBGE publishes Localidades do Brasil as public geoscience data. Preserve the
  dataset name, edition, source URL, and attribution in generated releases.
- OpenStreetMap explicit-postcode refinements are governed by ODbL and require
  OpenStreetMap contributor attribution and a derived-database share-alike
  assessment. Public Nominatim must never be bulk queried.

The machine-readable input boundary is `sources/lock.json`; its checksums and
rights-status fields are historical build evidence and are unchanged by the
private deployment decision. Public redistribution of the combined generated
artifact remains subject to ordinary release review plus applicable ODbL and
IBGE attribution/compliance requirements. See
[`docs/source-provenance.md`](docs/source-provenance.md) for the release gate.
