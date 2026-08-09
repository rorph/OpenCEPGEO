from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from opencepgeo.database import lookup as database_lookup
from opencepgeo.service import (
    DatasetUnavailable,
    ServiceConfig,
    ServiceState,
    create_server,
    verify_dataset,
)

from service_helpers import write_service_database


_VERSION = "fixture-service-v1"


def request(
    port: int,
    path: str,
    *,
    method: str = "GET",
) -> tuple[int, dict[str, object], dict[str, str]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        payload = json.loads(response.read())
        return response.status, payload, dict(response.getheaders())
    finally:
        connection.close()


def update_metadata(path: Path, **values: str) -> str:
    connection = sqlite3.connect(path)
    connection.executemany(
        "UPDATE metadata SET value = ? WHERE key = ?",
        ((value, key) for key, value in values.items()),
    )
    connection.commit()
    connection.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mutate_city_preserving_size_and_mtime(path: Path) -> os.stat_result:
    before = path.stat()
    time.sleep(0.002)
    needle = "São Paulo".encode()
    replacement = "São Paulu".encode()
    payload = path.read_bytes()
    offset = payload.find(needle)
    if offset < 0 or len(needle) != len(replacement):
        raise AssertionError("same-size fixture mutation target is unavailable")
    with path.open("r+b") as handle:
        handle.seek(offset)
        handle.write(replacement)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    if after.st_size != before.st_size:
        raise AssertionError("same-size fixture mutation changed SQLite file size")
    return before


def raw_request(port: int, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


def wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


class RunningService:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        request_timeout_seconds: float = 5.0,
        max_concurrent_requests: int = 64,
    ):
        self.server = create_server(
            config,
            request_timeout_seconds=request_timeout_seconds,
            max_concurrent_requests=max_concurrent_requests,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.log_output = io.StringIO()
        self.stderr_patch = mock.patch.object(sys, "stderr", self.log_output)

    def __enter__(self) -> int:
        self.stderr_patch.start()
        self.thread.start()
        return self.server.server_port

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.stderr_patch.stop()


class DatasetVerificationTests(unittest.TestCase):
    def test_verifies_hash_schema_version_and_offline_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dataset.sqlite"
            sha256 = write_service_database(database)
            dataset = verify_dataset(
                ServiceConfig(database, sha256, _VERSION, bind="127.0.0.1", port=0)
            )
            self.assertEqual(dataset.schema_version, "opencepgeo-sqlite-v4")
            self.assertEqual(dataset.dataset_version, _VERSION)
            with mock.patch.object(
                socket,
                "getaddrinfo",
                side_effect=AssertionError("DNS attempted during lookup"),
            ), mock.patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("network attempted during lookup"),
            ):
                result = dataset.find("01001000")
            self.assertEqual(result["city"], "São Paulo")

    def test_lookup_uses_read_only_immutable_sqlite_uri(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dataset.sqlite"
            write_service_database(database)
            real_connect = sqlite3.connect
            with mock.patch(
                "opencepgeo.database.sqlite3.connect", wraps=real_connect
            ) as connect:
                result = database_lookup(database, "01001000")
            self.assertEqual(result["cep"], "01001000")
            self.assertIn("mode=ro&immutable=1", connect.call_args.args[0])

    def test_same_size_mutation_with_restored_mtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dataset.sqlite"
            sha256 = write_service_database(database)
            dataset = verify_dataset(ServiceConfig(database, sha256, _VERSION))
            before = mutate_city_preserving_size_and_mtime(database)
            after = database.stat()
            self.assertEqual(after.st_ino, before.st_ino)
            self.assertEqual(after.st_size, before.st_size)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
            with self.assertRaises(DatasetUnavailable) as raised:
                dataset.find("01001000")
            self.assertEqual(raised.exception.problem.code, "dataset_changed")

    def test_mutation_during_lookup_is_rejected_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "dataset.sqlite"
            sha256 = write_service_database(database)
            dataset = verify_dataset(ServiceConfig(database, sha256, _VERSION))

            def lookup_then_mutate(path: Path, cep: str) -> dict[str, object]:
                result = database_lookup(path, cep)
                assert result is not None
                mutate_city_preserving_size_and_mtime(path)
                return result

            with mock.patch(
                "opencepgeo.service.lookup", side_effect=lookup_then_mutate
            ), self.assertRaises(DatasetUnavailable) as raised:
                dataset.find("01001000")
            self.assertEqual(raised.exception.problem.code, "dataset_changed")

    def test_startup_failures_have_distinct_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.sqlite"
            good_sha256 = write_service_database(good)
            bad_schema = root / "bad-schema.sqlite"
            bad_schema_sha256 = write_service_database(
                bad_schema, schema_version="opencepgeo-sqlite-v3"
            )
            corrupt = root / "corrupt.sqlite"
            corrupt.write_bytes(b"not sqlite")
            incomplete = root / "incomplete.sqlite"
            connection = sqlite3.connect(incomplete)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('format', 'opencepgeo-sqlite-v4');
                INSERT INTO metadata VALUES ('dataset_version', 'fixture-service-v1');
                CREATE TABLE cep_geo (cep TEXT PRIMARY KEY, dataset_version TEXT);
                """
            )
            connection.commit()
            connection.close()
            bad_count = root / "bad-count.sqlite"
            write_service_database(bad_count)
            bad_count_sha256 = update_metadata(
                bad_count,
                count_unique_ceps="3",
                count_located="2",
            )
            inconsistent_count = root / "inconsistent-count.sqlite"
            write_service_database(inconsistent_count)
            inconsistent_count_sha256 = update_metadata(
                inconsistent_count,
                count_unique_ceps="3",
            )
            invalid_count = root / "invalid-count.sqlite"
            write_service_database(invalid_count)
            invalid_count_sha256 = update_metadata(
                invalid_count,
                count_located="-1",
            )

            cases = (
                (
                    ServiceConfig(root / "missing.sqlite", "0" * 64, _VERSION),
                    "dataset_missing",
                ),
                (ServiceConfig(good, "", _VERSION), "dataset_checksum_unconfigured"),
                (
                    ServiceConfig(good, "f" * 64, _VERSION),
                    "dataset_checksum_mismatch",
                ),
                (
                    ServiceConfig(bad_schema, bad_schema_sha256, _VERSION),
                    "dataset_schema_incompatible",
                ),
                (
                    ServiceConfig(good, good_sha256, "different-version"),
                    "dataset_version_mismatch",
                ),
                (
                    ServiceConfig(
                        corrupt,
                        hashlib.sha256(corrupt.read_bytes()).hexdigest(),
                        _VERSION,
                    ),
                    "dataset_schema_incompatible",
                ),
                (
                    ServiceConfig(
                        incomplete,
                        hashlib.sha256(incomplete.read_bytes()).hexdigest(),
                        _VERSION,
                    ),
                    "dataset_schema_incompatible",
                ),
                (
                    ServiceConfig(good, good_sha256, ""),
                    "dataset_version_unconfigured",
                ),
                (
                    ServiceConfig(bad_count, bad_count_sha256, _VERSION),
                    "dataset_count_mismatch",
                ),
                (
                    ServiceConfig(invalid_count, invalid_count_sha256, _VERSION),
                    "dataset_metadata_invalid",
                ),
                (
                    ServiceConfig(
                        inconsistent_count,
                        inconsistent_count_sha256,
                        _VERSION,
                    ),
                    "dataset_count_mismatch",
                ),
            )
            for config, expected_code in cases:
                with self.subTest(expected_code=expected_code):
                    status, payload = ServiceState.load(config).readiness()
                    self.assertEqual(status, 503)
                    self.assertEqual(payload["error"]["code"], expected_code)


class ServiceAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "dataset.sqlite"
        self.sha256 = write_service_database(self.database)

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, path: Path | None = None) -> ServiceConfig:
        return ServiceConfig(
            path or self.database,
            self.sha256,
            _VERSION,
            bind="127.0.0.1",
            port=0,
        )

    def test_health_readiness_and_lookup_contracts(self):
        with RunningService(self.config()) as port:
            status, payload, headers = request(port, "/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["service"], "opencepgeo")
            self.assertEqual(headers["Cache-Control"], "no-store")

            status, payload, _headers = request(port, "/readyz")
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "ready")
            self.assertEqual(payload["dataset"]["sha256"], self.sha256)
            self.assertEqual(payload["dataset"]["dataset_version"], _VERSION)
            self.assertEqual(
                payload["dataset"]["counts"],
                {"unique_ceps": 6, "located": 5, "unresolved": 1},
            )

            status, payload, _headers = request(port, "/v1/cep/01001-000")
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "resolved")
            self.assertEqual(payload["data"]["cep"], "01001000")
            self.assertEqual(
                payload["data"]["geo"]["coordinates"], [-46.6333, -23.5505]
            )

            status, payload, _headers = request(port, "/v1/cep/99999999")
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "unresolved")
            self.assertIsNone(payload["data"]["geo"])
            self.assertEqual(payload["data"]["city"], "Cidade sem ponto")

            status, payload, prefix_headers = request(
                port, "/v1/cep/01001003/prefix"
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "resolved")
            prefix_data = payload["data"]
            self.assertEqual(
                set(prefix_data),
                {"prefix", "ibge", "dataset_version", "member_ceps", "geo"},
            )
            self.assertEqual(prefix_data["prefix"], "01001")
            self.assertEqual(prefix_data["ibge"], "3550308")
            self.assertEqual(prefix_data["dataset_version"], _VERSION)
            self.assertEqual(
                prefix_data["member_ceps"],
                ["01001000", "01001001", "01001002", "01001003"],
            )
            prefix_geo = prefix_data["geo"]
            self.assertEqual(prefix_geo["coordinates"], [-46.6333, -23.5505])
            self.assertEqual(prefix_geo["precision"], "observed_cep_prefix")
            self.assertEqual(
                prefix_geo["method"], "bounded_same_ibge_prefix_median"
            )
            self.assertEqual(prefix_geo["evidence_count"], 3)
            self.assertLessEqual(prefix_geo["evidence_radius_km"], 10.0)
            self.assertEqual(
                prefix_geo["source"], ["fixture", "openstreetmap"]
            )
            self.assertRegex(prefix_geo["evidence_digest"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                prefix_geo["evidence_digest"],
                "sha256:6c3c2e7d13365da1f1ca23e0a184a33c7ed0ac1065d5165c2b1a926722a73c71",
            )
            self.assertLessEqual(int(prefix_headers["Content-Length"]), 16 * 1024)

            repeated = request(port, "/v1/cep/01001003/prefix")[1]
            self.assertEqual(
                repeated["data"]["geo"]["evidence_digest"],
                prefix_geo["evidence_digest"],
            )

            status, payload, _headers = request(
                port, "/v1/cep/01001000/prefix"
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "unresolved")
            self.assertEqual(
                payload["data"]["member_ceps"],
                ["01001000", "01001001", "01001002", "01001003"],
            )
            self.assertIsNone(payload["data"]["geo"])

            status, payload, _headers = request(
                port, "/v1/cep/01001004/prefix"
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "unresolved")
            self.assertEqual(payload["data"]["ibge"], "3304557")
            self.assertEqual(payload["data"]["member_ceps"], ["01001004"])
            self.assertIsNone(payload["data"]["geo"])

            status, payload, _headers = request(
                port, "/v1/cep/11111111/prefix"
            )
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "cep_not_found")

            status, payload, _headers = request(port, "/v1/cep/abcdefgh")
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_cep")

            status, payload, _headers = request(
                port,
                "/v1/cep/%EF%BC%91%EF%BC%92%EF%BC%93%EF%BC%94"
                "%EF%BC%95%EF%BC%96%EF%BC%97%EF%BC%98",
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_cep")

            status, payload, _headers = request(port, "/v1/cep/11111111")
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "cep_not_found")

            for path in (
                "/v1/cep/01001000/extra",
                "/v1/cep/01001000/prefix/extra",
                "/v1/cep/01001000%2Fextra",
            ):
                with self.subTest(path=path):
                    status, payload, _headers = request(port, path)
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error"]["code"], "invalid_cep")

            status, payload, _headers = request(port, "/unknown")
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "route_not_found")

            status, payload, method_headers = request(port, "/healthz", method="POST")
            self.assertEqual(status, 405)
            self.assertEqual(payload["error"]["code"], "method_not_allowed")
            self.assertEqual(method_headers["Allow"], "GET")

    def test_access_log_never_contains_requested_cep_or_path(self):
        service = RunningService(self.config())
        with service as port:
            self.assertEqual(request(port, "/v1/cep/01001000")[0], 200)
        output = service.log_output.getvalue()
        self.assertNotIn("01001000", output)
        self.assertNotIn("/v1/cep/", output)
        event = json.loads(output.strip())
        self.assertEqual(
            event,
            {"client": "127.0.0.1", "method": "GET", "status": 200},
        )

    def test_malformed_request_returns_generic_json_without_reflection(self):
        service = RunningService(self.config())
        sensitive_path = b"/v1/cep/01001000"
        with service as port:
            response = raw_request(
                port,
                b"GET " + sensitive_path + b" EXTRA HTTP/1.1\r\n\r\n",
            )
        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b" 400 ", headers.splitlines()[0])
        self.assertIn(b"application/json", headers)
        self.assertIn(b"Connection: close", headers)
        self.assertNotIn(b"text/html", headers)
        self.assertNotIn(sensitive_path, response)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "malformed_request")
        logs = service.log_output.getvalue()
        self.assertNotIn("01001000", logs)
        self.assertNotIn("/v1/cep/", logs)

        service = RunningService(self.config())
        with service as port:
            response = raw_request(
                port,
                sensitive_path + b" / HTTP/1.1\r\nHost: local\r\n\r\n",
            )
        self.assertNotIn(sensitive_path, response)
        self.assertNotIn("01001000", service.log_output.getvalue())
        self.assertIn('"method": "unknown"', service.log_output.getvalue())

    def test_slow_client_times_out_and_concurrency_is_bounded(self):
        service = RunningService(
            self.config(),
            request_timeout_seconds=0.2,
            max_concurrent_requests=1,
        )
        with service as port:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as slow:
                slow.sendall(b"GET /healthz HTTP/1.1\r\nHost: local")
                wait_until(lambda: service.server.active_requests == 1)

                overloaded = socket.create_connection(("127.0.0.1", port), timeout=2)
                try:
                    overloaded.sendall(
                        b"GET /healthz HTTP/1.1\r\nHost: local\r\n\r\n"
                    )
                    try:
                        rejected = overloaded.recv(1024)
                    except ConnectionResetError:
                        rejected = b""
                    self.assertEqual(rejected, b"")
                finally:
                    overloaded.close()

                slow.settimeout(1)
                try:
                    timed_out = slow.recv(1024)
                except ConnectionResetError:
                    timed_out = b""
                self.assertEqual(timed_out, b"")
                wait_until(lambda: service.server.active_requests == 0)
                self.assertEqual(request(port, "/healthz")[0], 200)

    def test_unavailable_service_stays_live_and_fails_closed(self):
        missing = self.root / "missing.sqlite"
        with RunningService(self.config(missing)) as port:
            self.assertEqual(request(port, "/healthz")[0], 200)
            status, payload, _headers = request(port, "/readyz")
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "dataset_missing")
            status, payload, _headers = request(port, "/v1/cep/01001000")
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "dataset_missing")

    def test_artifact_change_revokes_readiness(self):
        with RunningService(self.config()) as port:
            self.assertEqual(request(port, "/readyz")[0], 200)
            with self.database.open("ab") as handle:
                handle.write(b"changed")
            status, payload, _headers = request(port, "/readyz")
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "dataset_changed")
            status, payload, _headers = request(port, "/v1/cep/01001000")
            self.assertEqual(status, 503)
            self.assertEqual(payload["error"]["code"], "dataset_changed")

    def test_prefix_aggregate_over_ten_kilometers_is_not_promoted(self):
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE cep_geo SET latitude = ?, longitude = ? WHERE cep = ?",
            (-22.0, -44.0, "01001002"),
        )
        connection.commit()
        connection.close()
        self.sha256 = hashlib.sha256(self.database.read_bytes()).hexdigest()

        with RunningService(self.config()) as port:
            status, payload, _headers = request(
                port, "/v1/cep/01001003/prefix"
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "unresolved")
        self.assertEqual(len(payload["data"]["member_ceps"]), 4)
        self.assertIsNone(payload["data"]["geo"])

    def test_concurrent_requests_use_independent_read_only_lookups(self):
        with RunningService(self.config()) as port:
            paths = (
                ["/v1/cep/01001000", "/v1/cep/99999999", "/v1/cep/11111111"]
                * 24
            )
            with ThreadPoolExecutor(max_workers=16) as executor:
                results = list(executor.map(lambda path: request(port, path), paths))
            expected = {
                "/v1/cep/01001000": (200, "resolved"),
                "/v1/cep/99999999": (200, "unresolved"),
                "/v1/cep/11111111": (404, "not_found"),
            }
            for path, (status, payload, _headers) in zip(paths, results):
                self.assertEqual((status, payload["status"]), expected[path])


if __name__ == "__main__":
    unittest.main()
