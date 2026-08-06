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
- OpenStreetMap-derived refinements, if added later, are governed by ODbL and
  require attribution. Public Nominatim must never be bulk queried.

The build records input versions in the SQLite `metadata` table, but users are
responsible for confirming that their intended acquisition, use, and
redistribution of each input and derived artifact is permitted.

