# Internal lookup service runbook

The lookup image contains code only. It never downloads, builds, updates, or
publishes a dataset. Mount one immutable SQLite v4 artifact at runtime and pin
both its version and SHA-256.

## API contract

All responses are JSON with `Cache-Control: no-store`.

| Endpoint | HTTP | Contract |
|---|---:|---|
| `GET /healthz` | 200 | `status: ok`; process liveness only |
| `GET /readyz` | 200 | `status: ready` plus schema, dataset version, SHA-256, verified counts, and observational freshness |
| `GET /readyz` | 503 | `status: unavailable` plus a stable dataset error code |
| `GET /v1/cep/{cep}` | 200 | `status: resolved` or `unresolved`, with the existing lookup row under `data` |
| `GET /v1/cep/{cep}` | 400 | `invalid_cep` |
| `GET /v1/cep/{cep}` | 404 | `cep_not_found` |
| `GET /v1/cep/{cep}` | 503 | the startup/runtime dataset-unavailable error |
| `GET /v1/cep/{cep}/prefix` | 200 | same-prefix/same-IBGE membership plus a safe aggregate `geo`, or `geo: null` |
| `GET /v1/cep/{cep}/prefix` | 400/404/503 | the same invalid, absent, and dataset-unavailable errors as exact lookup |

Dataset error codes are `dataset_checksum_unconfigured`,
`dataset_version_unconfigured`, `dataset_missing`, `dataset_unavailable`,
`dataset_checksum_mismatch`, `dataset_schema_incompatible`,
`dataset_version_mismatch`, `dataset_metadata_invalid`,
`dataset_count_mismatch`, `dataset_corrupt`, and `dataset_changed`.

`/readyz` never fails on dataset age. A stale but otherwise valid artifact
stays `status: ready` and continues to serve lookups. Freshness is
observational so a missed weekly refresh cannot take FarmaBarato CEP
resolution down.

A ready payload includes this additive `dataset.freshness` object:

```json
{
  "dnec_published_at": "2026-08-11T00:00:00Z",
  "captured_at": "2026-08-11T01:00:00Z",
  "built_at": "2026-08-11T02:00:00Z",
  "age_seconds": 298800,
  "age_source": "captured_at"
}
```

Timestamps are timezone-aware ISO-8601 normalized to UTC `Z`. Optional
SQLite metadata keys `dnec_published_at`, `captured_at`, and `built_at`
are used when they parse; naive or invalid values become `null` and do
not revoke readiness. When `built_at` is absent, it is the artifact
mtime. `age_seconds` is `now` minus the first available of `captured_at`,
`dnec_published_at`, then `built_at`, and `age_source` names that field.
Current artifacts that do not persist publication metadata therefore
report `age_source: built_at` from file mtime.

Access logs contain only client IP, HTTP method, and response status. They never
record the request path, request line, CEP, query string, or formatter arguments.
Unknown or malformed method tokens are logged as `unknown`. Parser failures use
generic JSON, never inherited HTML or reflected request text, and close the
connection. Accepted sockets have a five-second inactivity timeout and the
process admits at most 64 request threads; excess connections are closed.

The prefix response `data` object has exactly these fields:

```json
{
  "prefix": "01001",
  "ibge": "3550308",
  "dataset_version": "2026.2.1-rc2",
  "member_ceps": ["01001000", "01001001", "01001002"],
  "geo": {
    "type": "Point",
    "coordinates": [-46.6333, -23.5505],
    "precision": "observed_cep_prefix",
    "method": "bounded_same_ibge_prefix_median",
    "evidence_count": 3,
    "evidence_radius_km": 0.2,
    "source": ["first-party", "openstreetmap"],
    "evidence_digest": "sha256:<64 lowercase hexadecimal characters>"
  }
}
```

`member_ceps` is the sorted set of every dataset CEP with both the target's
five-digit prefix and seven-digit IBGE code, including the target. An eight-digit
CEP permits at most 1,000 members for one prefix, and the complete HTTP response
is capped at 16 KiB. The aggregate uses only member rows whose precision is
`observed_cep` or `osm_postcode` and are not the requested CEP itself; it
requires at least three rows, uses the coordinate-wise median, and is returned
only when every included point is at most 10 km from that median. Municipality,
existing prefix, and unresolved rows can establish membership but never
contribute coordinates. If a safety gate fails, the endpoint remains 200 with
`status: unresolved` and `geo: null`.

## Prepare an immutable artifact

Use the release verifier before deployment. Copy the selected SQLite file to
its version-specific durable release directory; never overwrite a prior path.

```bash
opencepgeo release verify /srv/import/opencepgeo-2026.2.1-rc2
test ! -e \
  /srv/opencepgeo/releases/2026.2.1-rc2/opencepgeo-2026.2.1-rc2.sqlite
install -D -m 0444 \
  /srv/import/opencepgeo-2026.2.1-rc2/opencepgeo-2026.2.1-rc2.sqlite \
  /srv/opencepgeo/releases/2026.2.1-rc2/opencepgeo-2026.2.1-rc2.sqlite
sha256sum \
  /srv/opencepgeo/releases/2026.2.1-rc2/opencepgeo-2026.2.1-rc2.sqlite
```

Configure the full 64-character digest in `OPENCEPGEO_DATABASE_SHA256`. Keep
the release directory and canonical packaged filename unchanged so the durable
path identifies the exact RC2 contract.

## Internal-only Docker Compose example

The consumer must join the deployment network named `px-cep-internal`;
there is intentionally no `ports` entry.

```yaml
services:
  opencepgeo:
    image: opencepgeo:git-${OPENCEPGEO_SERVICE_COMMIT:?set full source commit}
    restart: unless-stopped
    user: "65532:65532"
    read_only: true
    pids_limit: 128
    mem_limit: 256m
    cpus: 1.0
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    environment:
      OPENCEPGEO_DATABASE: /data/opencepgeo.sqlite
      OPENCEPGEO_DATABASE_SHA256: ${OPENCEPGEO_DATABASE_SHA256:?set SQLite SHA-256}
      OPENCEPGEO_DATASET_VERSION: ${OPENCEPGEO_DATASET_VERSION:?set dataset version}
    volumes:
      - type: bind
        source: /srv/opencepgeo/releases/2026.2.1-rc2/opencepgeo-2026.2.1-rc2.sqlite
        target: /data/opencepgeo.sqlite
        read_only: true
    networks:
      - cep-internal

networks:
  cep-internal:
    name: px-cep-internal
    internal: true
    ipam:
      config:
        - subnet: 10.215.0.0/24
```

Set `OPENCEPGEO_SERVICE_COMMIT` to the full source commit and enforce
append-only commit tags in the image registry. Deployments may additionally pin
the built image manifest digest; never reuse a commit tag for different bytes.

A separately deployed FarmaBarato Compose project joins the same stable network
by declaring it external instead of allowing Compose to create a project-scoped
replacement:

```yaml
services:
  px-api:
    networks:
      - cep-internal

networks:
  cep-internal:
    external: true
    name: px-cep-internal
```

Do not add a host port. Probe readiness from inside the container or from an
authorized consumer on the same internal network:

```bash
docker compose exec opencepgeo \
  python -m opencepgeo.service --check-ready
```

## Replacement

1. Verify and install the new artifact at a new immutable path.
2. Record its full SHA-256 and dataset version in deployment configuration.
3. Recreate the service container with the new mount, hash, and version.
4. Require `/readyz` to return 200 and the intended version/hash.
5. Run exact known, unresolved, invalid, and unknown CEP probes from the
   internal network.
6. Keep the previous immutable artifact and configuration until application
   validation completes.

Never replace or edit the mounted SQLite path in place. Every lookup opens
SQLite with `mode=ro&immutable=1` and verifies device, inode, size, mtime, and
ctime both before and after the query, including its error path. Any identity
change revokes readiness before data can be returned.

## Rollback

1. Restore the prior immutable service image commit tag or manifest digest,
   artifact path, full SHA-256, and dataset version together.
2. Recreate the container; do not reuse the process that observed another file.
3. Verify `/readyz` reports the prior version/hash and repeat the exact CEP
   probes.
4. Retain the rejected artifact for investigation; do not publish or deploy it
   elsewhere based on this rollback alone.

## Failure triage

- `/healthz` fails: process/container failure.
- `/healthz` passes and `/readyz` returns 503: inspect the stable error code and
  container startup log; do not route lookup traffic to it.
- `/readyz` is 200 with a large `dataset.freshness.age_seconds`: the artifact
  is old but still serving. Do not recycle the container for age alone; the
  weekly refresh worker and its monitor own the alert.
- `dataset_checksum_mismatch`: verify the mount target and deployment digest.
- `dataset_schema_incompatible`: select a SQLite v4 artifact or deploy a
  compatible service version.
- `dataset_version_mismatch`: update path, hash, and version as one atomic
  deployment change.
- `dataset_changed`: remove the container, restore an immutable artifact path,
  and start a new container.

Lookup never performs DNS or reaches a fallback service. Network errors from a
consumer therefore indicate service routing, not CEP data acquisition.
