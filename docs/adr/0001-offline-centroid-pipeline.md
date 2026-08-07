# ADR 0001: Build versioned offline CEP centroid artifacts

- Status: accepted for MVP
- Date: 2026-08-06

## Context

OpenCEP provides broad CEP-to-address coverage but no coordinates. BrasilAPI
adds coordinates by querying public Nominatim at request time, which introduces
availability, rate-limit, reproducibility, and precision problems. Downstream
systems need a local lookup that reports the quality of every estimate.

## Options considered

1. **Proxy several public CEP/geocoding APIs.** Small implementation, but retains
   runtime dependency and produces results that change without a dataset version.
2. **Publish a standalone lookup API backed by a private database.** Reliable at
   runtime but still makes every consumer depend on one service deployment.
3. **Build versioned SQLite artifacts from pinned datasets.** Consumers operate
   offline, builds are auditable, and an API can be layered on later without
   changing the data contract.

## Decision

Use option 3. The pipeline streams OpenCEP records into SQLite and estimates a
point using trusted exact observations, locally extracted explicit-postcode
OSM points contained by the CEP's locked official municipality polygon and
corroborated against its IBGE municipality reference point,
safe same-municipality prefix groups, then the official IBGE municipality
point. It never interpolates numeric CEPs and never calls a third-party service
during lookup.

The public contract uses valid GeoJSON coordinate order and records precision,
method, evidence count, retained-evidence radius, bounded source categories, a
fixed evidence digest, and dataset version. The radius is not a calibrated
position-error bound. Individual evidence IDs stay in the checksum-locked
input rather than growing each output row. Unknown locations remain null.

## Consequences

- Downstream projects can import or query one immutable local artifact.
- Municipality fallbacks are deliberately coarse, especially for large rural
  municipalities; consumers must expose or honor `precision`.
- Prefix centroids improve automatically as first-party observations grow.
- Production first-party rows require stable source-owned evidence identities;
  conflicting reuse fails closed.
- IBGE Malha Municipal 2024 supplies the primary containment polygons. The
  IBGE Localidades reference-point distance remains an independent coarse
  safety backstop rather than a substitute for containment.
- Dataset acquisition and redistribution rights remain a release gate.
- A pinned local OSM extract may add an explicit-postcode precision tier. The
  build never uses public Nominatim or ambiguous street-only evidence.
