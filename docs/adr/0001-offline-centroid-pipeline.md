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
point using trusted exact observations, safe same-municipality prefix groups,
then the official IBGE municipality point. It never interpolates numeric CEPs
and never calls a third-party service during lookup.

The public contract uses valid GeoJSON coordinate order and records precision,
sample count, radius, and source. Unknown locations remain null.

## Consequences

- Downstream projects can import or query one immutable local artifact.
- Municipality fallbacks are deliberately coarse, especially for large rural
  municipalities; consumers must expose or honor `precision`.
- Prefix centroids improve automatically as first-party observations grow.
- Dataset acquisition and redistribution rights remain a release gate.
- A future self-hosted OSM/Nominatim refinement can add another precision tier
  without introducing public runtime calls.

