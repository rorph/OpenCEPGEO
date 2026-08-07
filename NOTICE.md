# Data provenance and licensing notice

OpenCEPGeo's source code is MIT licensed. Input datasets and generated database
artifacts may be governed by different terms.

- OpenCEP publishes a downloadable CEP corpus from its GitHub releases and
  labels its repository MIT. It does not provide a separate, explicit database
  license or a reproducible upstream extraction process.
- Correios describes the Diretório Nacional de Endereços (DNE) as its official
  commercial address database and asserts rights over that database.
- IBGE publishes Localidades do Brasil as public geoscience data. Preserve the
  dataset name, edition, source URL, and attribution in generated releases.
- OpenStreetMap explicit-postcode refinements are governed by ODbL and require
  OpenStreetMap contributor attribution and a derived-database share-alike
  assessment. Public Nominatim must never be bulk queried.

The machine-readable input boundary is `sources/lock.json`; its checksums and
rights-status fields are evidence, not a license grant. Public redistribution
of source archives or generated CEP data is blocked until the OpenCEP/DNE
rights question is explicitly cleared. See
[`docs/source-provenance.md`](docs/source-provenance.md) for the release gate.
