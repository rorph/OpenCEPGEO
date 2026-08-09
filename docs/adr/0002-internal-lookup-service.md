# ADR 0002: Add an internal-only lookup service over immutable artifacts

- Status: accepted
- Date: 2026-08-08

## Context

ADR 0001 deliberately made the versioned SQLite artifact the primary product
boundary. FarmaBarato needs a production lookup boundary without embedding the
artifact in its application container and without restoring runtime calls to
BrasilAPI, ViaCEP, OpenCEP, Correios, Nominatim, or another network data source.

The release artifact is immutable and checksum-addressed. The service must not
build, download, modify, or publish it. Health checks must also distinguish a
live process from one that has rejected its configured dataset.

## Options considered

1. **Mount SQLite directly into every consumer.** This keeps lookup local but
   duplicates artifact rollout and schema-verification logic across services.
2. **Import every release into a shared PostgreSQL instance.** This is useful
   for bulk consumers but adds a second mutable storage lifecycle for a simple
   exact-key lookup.
3. **Run a small internal HTTP service over a read-only SQLite mount.** This
   centralizes startup verification and the stable API while preserving the
   immutable offline artifact contract.

## Decision

Use option 3 for consumers that need an isolated lookup boundary. The service
uses only the Python standard library and the existing SQLite lookup contract.
At startup it verifies the configured SHA-256, SQLite integrity, schema
metadata, configured dataset version, and row-version consistency. A failed
verification also rejects missing, malformed, internally inconsistent, or
row-mismatched total/located/unresolved metadata counts. The process remains
live but unready; every lookup fails closed with the corresponding
dataset-unavailable code.

The exact API wraps the existing row without altering it. A row with `geo: null`
is returned as `status: unresolved`; it is not converted into a not-found result
and no network fallback is attempted. The bounded prefix API returns every
sorted same-prefix/same-IBGE CEP (at most 1,000) and only a strict aggregate,
never raw rows. Its median centroid uses at least three `observed_cep` or
`osm_postcode` rows and is omitted when its radius exceeds 10 km. Municipality
coordinates are never promoted by this endpoint.

Every lookup opens SQLite with `mode=ro&immutable=1` and verifies device, inode,
size, mtime, and ctime immediately before and after the read, including the
query-error path. A changed identity takes precedence over a generic query
failure and no result from that query is returned.

The container is data-agnostic and runs as UID/GID 65532. Deployment must add a
read-only, immutable SQLite bind mount, read-only root filesystem, all
capabilities dropped, `no-new-privileges`, an internal Docker network, and no
host-published port.

Access logging records only client IP, HTTP method, and response status. CEPs,
request paths, request lines, query strings, and formatter arguments are never
written to the access log.
Malformed requests receive a generic JSON error with a closed connection.
Accepted sockets have a five-second inactivity timeout, and a semaphore limits
the process to 64 concurrent request threads.

## Consequences

- Consumers share one versioned, stable internal API and do not need SQLite
  schema knowledge.
- Liveness remains independent of dataset readiness, so orchestration can
  report checksum, version, schema, and availability failures distinctly.
- Artifact replacement requires a new container start. Mutating or replacing a
  mounted file in place revokes readiness and is unsupported.
- This service is optional. Bulk import consumers may continue to use verified
  release files directly under ADR 0001.
- The internal network is a deployment control, not authentication. Any future
  cross-host exposure requires a separate security design and is out of scope.
