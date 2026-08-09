from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn
from urllib.parse import unquote, urlsplit

from . import __version__
from .database import LOOKUP_COLUMNS, SCHEMA_VERSION, lookup, lookup_prefix
from .estimator import normalize_cep


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASCII_CEP = re.compile(r"^[0-9]{8}$")
_NONNEGATIVE_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")
_LOGGABLE_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)
_DEFAULT_DATABASE = "/data/opencepgeo.sqlite"
_DEFAULT_BIND = "0.0.0.0"
_DEFAULT_PORT = 8080
_REQUEST_TIMEOUT_SECONDS = 5.0
_MAX_CONCURRENT_REQUESTS = 64
_MAX_DATASET_VERSION_BYTES = 128
_MAX_PREFIX_RESPONSE_BYTES = 16 * 1024


@dataclass(frozen=True)
class ServiceConfig:
    database_path: Path
    expected_sha256: str
    expected_dataset_version: str
    bind: str = _DEFAULT_BIND
    port: int = _DEFAULT_PORT


@dataclass(frozen=True)
class DatasetProblem:
    code: str
    message: str
    detail: str | None = None

    def response(self) -> dict[str, object]:
        return {
            "status": "unavailable",
            "error": {"code": self.code, "message": self.message},
        }


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def read(cls, path: Path) -> FileIdentity:
        stat = path.stat()
        return cls(
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )


@dataclass(frozen=True)
class VerifiedDataset:
    path: Path
    sha256: str
    schema_version: str
    dataset_version: str
    unique_ceps: int
    located: int
    unresolved: int
    identity: FileIdentity

    def ensure_unchanged(self) -> None:
        try:
            current = FileIdentity.read(self.path)
        except OSError as exc:
            raise DatasetUnavailable(
                DatasetProblem(
                    "dataset_unavailable",
                    "The verified CEP dataset is no longer available.",
                    str(exc),
                )
            ) from exc
        if current != self.identity:
            raise DatasetUnavailable(
                DatasetProblem(
                    "dataset_changed",
                    "The CEP dataset changed after startup verification.",
                )
            )

    def find(self, cep: str) -> dict[str, object] | None:
        self.ensure_unchanged()
        try:
            result = lookup(self.path, cep)
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            self.ensure_unchanged()
            raise DatasetUnavailable(
                DatasetProblem(
                    "dataset_unavailable",
                    "The verified CEP dataset could not be queried.",
                    str(exc),
                )
            ) from exc
        self.ensure_unchanged()
        return result

    def find_prefix(self, cep: str) -> dict[str, object] | None:
        self.ensure_unchanged()
        try:
            result = lookup_prefix(self.path, cep)
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            self.ensure_unchanged()
            raise DatasetUnavailable(
                DatasetProblem(
                    "dataset_unavailable",
                    "The verified CEP dataset could not be queried.",
                    str(exc),
                )
            ) from exc
        self.ensure_unchanged()
        return result

    def ready_response(self) -> dict[str, object]:
        self.ensure_unchanged()
        return {
            "status": "ready",
            "dataset": {
                "schema_version": self.schema_version,
                "dataset_version": self.dataset_version,
                "sha256": self.sha256,
                "counts": {
                    "unique_ceps": self.unique_ceps,
                    "located": self.located,
                    "unresolved": self.unresolved,
                },
            },
        }


class DatasetUnavailable(RuntimeError):
    def __init__(self, problem: DatasetProblem):
        super().__init__(problem.detail or problem.message)
        self.problem = problem


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_uri(path: Path) -> str:
    return f"file:{path.resolve()}?mode=ro&immutable=1"


def _metadata_count(metadata: dict[str, str], key: str) -> int:
    raw_value = metadata.get(key, "")
    if not _NONNEGATIVE_INTEGER.fullmatch(raw_value):
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_metadata_invalid",
                "The CEP dataset count metadata is missing or invalid.",
                key,
            )
        )
    return int(raw_value)


def verify_dataset(config: ServiceConfig) -> VerifiedDataset:
    expected_sha256 = config.expected_sha256.strip().lower()
    expected_version = config.expected_dataset_version.strip()
    if not _SHA256.fullmatch(expected_sha256):
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_checksum_unconfigured",
                "A valid expected dataset SHA-256 is required.",
            )
        )
    if (
        not expected_version
        or len(expected_version.encode("utf-8")) > _MAX_DATASET_VERSION_BYTES
    ):
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_version_unconfigured",
                "An expected dataset version is required.",
            )
        )

    path = config.database_path.resolve()
    try:
        if not path.is_file():
            raise DatasetUnavailable(
                DatasetProblem(
                    "dataset_missing",
                    "The configured CEP dataset is missing.",
                )
            )
        identity = FileIdentity.read(path)
        actual_sha256 = _sha256(path)
    except DatasetUnavailable:
        raise
    except OSError as exc:
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_unavailable",
                "The configured CEP dataset cannot be read.",
                str(exc),
            )
        ) from exc

    if actual_sha256 != expected_sha256:
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_checksum_mismatch",
                "The CEP dataset SHA-256 does not match the configured value.",
            )
        )

    try:
        connection = sqlite3.connect(_database_uri(path), uri=True)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            schema_version = metadata.get("format", "")
            dataset_version = metadata.get("dataset_version", "")
            if schema_version != SCHEMA_VERSION:
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_schema_incompatible",
                        "The CEP dataset schema is incompatible with this service.",
                        f"found {schema_version!r}; expected {SCHEMA_VERSION!r}",
                    )
                )
            if (
                not dataset_version
                or len(dataset_version.encode("utf-8")) > _MAX_DATASET_VERSION_BYTES
                or dataset_version != expected_version
            ):
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_version_mismatch",
                        "The CEP dataset version does not match the configured value.",
                        f"found {dataset_version!r}; expected {expected_version!r}",
                    )
                )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(cep_geo)")
            }
            missing_columns = set(LOOKUP_COLUMNS) - columns
            if missing_columns:
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_schema_incompatible",
                        "The CEP dataset is missing required lookup fields.",
                        ", ".join(sorted(missing_columns)),
                    )
                )
            unique_ceps = _metadata_count(metadata, "count_unique_ceps")
            located = _metadata_count(metadata, "count_located")
            unresolved = _metadata_count(metadata, "count_unresolved")
            if unique_ceps != located + unresolved:
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_count_mismatch",
                        "The CEP dataset count metadata is inconsistent.",
                    )
                )
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_corrupt",
                        "The CEP dataset failed its SQLite integrity check.",
                        repr(integrity),
                    )
                )
            actual_total, mismatched_rows, actual_located = connection.execute(
                """
                SELECT
                    count(*),
                    sum(CASE WHEN dataset_version IS NULL OR dataset_version != ?
                        THEN 1 ELSE 0 END),
                    sum(CASE WHEN latitude IS NOT NULL THEN 1 ELSE 0 END)
                FROM cep_geo
                """,
                (dataset_version,),
            ).fetchone()
            mismatched_rows = mismatched_rows or 0
            actual_located = actual_located or 0
            if mismatched_rows:
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_version_mismatch",
                        "CEP rows do not match the dataset metadata version.",
                        f"{mismatched_rows} rows differ",
                    )
                )
            actual_unresolved = actual_total - actual_located
            if (actual_total, actual_located, actual_unresolved) != (
                unique_ceps,
                located,
                unresolved,
            ):
                raise DatasetUnavailable(
                    DatasetProblem(
                        "dataset_count_mismatch",
                        "The CEP dataset rows do not match the declared counts.",
                        "declared "
                        f"{unique_ceps}/{located}/{unresolved}; actual "
                        f"{actual_total}/{actual_located}/{actual_unresolved}",
                    )
                )
        finally:
            connection.close()
    except DatasetUnavailable:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_schema_incompatible",
                "The CEP dataset is not a compatible SQLite artifact.",
                str(exc),
            )
        ) from exc

    try:
        if FileIdentity.read(path) != identity:
            raise DatasetUnavailable(
                DatasetProblem(
                    "dataset_changed",
                    "The CEP dataset changed during startup verification.",
                )
            )
    except OSError as exc:
        raise DatasetUnavailable(
            DatasetProblem(
                "dataset_unavailable",
                "The configured CEP dataset is no longer available.",
                str(exc),
            )
        ) from exc

    return VerifiedDataset(
        path=path,
        sha256=actual_sha256,
        schema_version=SCHEMA_VERSION,
        dataset_version=dataset_version,
        unique_ceps=unique_ceps,
        located=located,
        unresolved=unresolved,
        identity=identity,
    )


class ServiceState:
    def __init__(
        self,
        dataset: VerifiedDataset | None,
        problem: DatasetProblem | None,
    ) -> None:
        if (dataset is None) == (problem is None):
            raise ValueError("exactly one of dataset or problem is required")
        self._dataset = dataset
        self._problem = problem
        self._lock = threading.Lock()

    @classmethod
    def load(cls, config: ServiceConfig) -> ServiceState:
        try:
            return cls(verify_dataset(config), None)
        except DatasetUnavailable as exc:
            return cls(None, exc.problem)

    def _unavailable(self, problem: DatasetProblem) -> DatasetProblem:
        with self._lock:
            self._dataset = None
            self._problem = problem
        return problem

    def readiness(self) -> tuple[int, dict[str, object]]:
        with self._lock:
            dataset = self._dataset
            problem = self._problem
        if dataset is None:
            assert problem is not None
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()
        try:
            return HTTPStatus.OK, dataset.ready_response()
        except DatasetUnavailable as exc:
            problem = self._unavailable(exc.problem)
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()

    def lookup(self, raw_cep: str) -> tuple[int, dict[str, object]]:
        cep = normalize_cep(raw_cep)
        if cep is None or not _ASCII_CEP.fullmatch(cep):
            return HTTPStatus.BAD_REQUEST, {
                "status": "invalid",
                "error": {
                    "code": "invalid_cep",
                    "message": "CEP must normalize to exactly 8 digits.",
                },
            }

        with self._lock:
            dataset = self._dataset
            problem = self._problem
        if dataset is None:
            assert problem is not None
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()
        try:
            result = dataset.find(cep)
        except DatasetUnavailable as exc:
            problem = self._unavailable(exc.problem)
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()
        if result is None:
            return HTTPStatus.NOT_FOUND, {
                "status": "not_found",
                "error": {
                    "code": "cep_not_found",
                    "message": "CEP is not present in the configured dataset.",
                },
            }
        return HTTPStatus.OK, {
            "status": "resolved" if result["geo"] is not None else "unresolved",
            "data": result,
        }

    def lookup_prefix(self, raw_cep: str) -> tuple[int, dict[str, object]]:
        cep = normalize_cep(raw_cep)
        if cep is None or not _ASCII_CEP.fullmatch(cep):
            return HTTPStatus.BAD_REQUEST, {
                "status": "invalid",
                "error": {
                    "code": "invalid_cep",
                    "message": "CEP must normalize to exactly 8 digits.",
                },
            }

        with self._lock:
            dataset = self._dataset
            problem = self._problem
        if dataset is None:
            assert problem is not None
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()
        try:
            result = dataset.find_prefix(cep)
        except DatasetUnavailable as exc:
            problem = self._unavailable(exc.problem)
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()
        if result is None:
            return HTTPStatus.NOT_FOUND, {
                "status": "not_found",
                "error": {
                    "code": "cep_not_found",
                    "message": "CEP is not present in the configured dataset.",
                },
            }
        response = {
            "status": "resolved" if result["geo"] is not None else "unresolved",
            "data": result,
        }
        body_size = len(
            (
                json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        if body_size > _MAX_PREFIX_RESPONSE_BYTES:
            problem = self._unavailable(
                DatasetProblem(
                    "dataset_unavailable",
                    "The verified CEP prefix response exceeds its safety bound.",
                )
            )
            return HTTPStatus.SERVICE_UNAVAILABLE, problem.response()
        return HTTPStatus.OK, response


class LookupHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        state: ServiceState,
        *,
        request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
        max_concurrent_requests: int = _MAX_CONCURRENT_REQUESTS,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be positive")
        self.state = state
        self.request_timeout_seconds = request_timeout_seconds
        self.max_concurrent_requests = max_concurrent_requests
        self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)
        self._active_requests = 0
        self._active_requests_lock = threading.Lock()
        super().__init__(server_address, LookupRequestHandler)

    @property
    def active_requests(self) -> int:
        with self._active_requests_lock:
            return self._active_requests

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout_seconds)
        return request, client_address

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_requests_lock:
            self._active_requests += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_requests_lock:
                self._active_requests -= 1
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_requests_lock:
                self._active_requests -= 1
            self._request_slots.release()


class LookupRequestHandler(BaseHTTPRequestHandler):
    server: LookupHTTPServer
    server_version = "OpenCEPGeo"
    sys_version = ""

    def _write_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        self.close_connection = True
        error_code = {
            HTTPStatus.BAD_REQUEST: "malformed_request",
            HTTPStatus.REQUEST_URI_TOO_LONG: "request_uri_too_long",
            HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE: "request_headers_too_large",
            HTTPStatus.HTTP_VERSION_NOT_SUPPORTED: "http_version_not_supported",
        }.get(code, "request_error")
        try:
            self._write_json(
                code,
                {
                    "status": "invalid",
                    "error": {
                        "code": error_code,
                        "message": "The HTTP request could not be processed.",
                    },
                },
                headers={"Connection": "close"},
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "opencepgeo",
                    "version": __version__,
                },
            )
            return
        if parsed.path == "/readyz":
            status, payload = self.server.state.readiness()
            self._write_json(status, payload)
            return
        prefix = "/v1/cep/"
        if parsed.path.startswith(prefix):
            raw_cep = unquote(parsed.path[len(prefix) :])
            if raw_cep.endswith("/prefix") and raw_cep.count("/") == 1:
                status, payload = self.server.state.lookup_prefix(raw_cep[:-7])
            elif not raw_cep or "/" in raw_cep:
                status, payload = self.server.state.lookup("")
            else:
                status, payload = self.server.state.lookup(raw_cep)
            self._write_json(status, payload)
            return
        self._write_json(
            HTTPStatus.NOT_FOUND,
            {
                "status": "not_found",
                "error": {
                    "code": "route_not_found",
                    "message": "The requested route does not exist.",
                },
            },
        )

    def _method_not_allowed(self) -> None:
        self._write_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {
                "status": "invalid",
                "error": {
                    "code": "method_not_allowed",
                    "message": "Only GET is supported.",
                },
            },
            headers={"Allow": "GET"},
        )

    do_DELETE = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_TRACE = _method_not_allowed

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        try:
            status: int | str = int(code)
        except (TypeError, ValueError):
            status = "unknown"
        event = {
            "client": self.client_address[0],
            "method": (
                self.command if self.command in _LOGGABLE_METHODS else "unknown"
            ),
            "status": status,
        }
        print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    config: ServiceConfig,
    *,
    request_timeout_seconds: float = _REQUEST_TIMEOUT_SECONDS,
    max_concurrent_requests: int = _MAX_CONCURRENT_REQUESTS,
) -> LookupHTTPServer:
    state = ServiceState.load(config)
    return LookupHTTPServer(
        (config.bind, config.port),
        state,
        request_timeout_seconds=request_timeout_seconds,
        max_concurrent_requests=max_concurrent_requests,
    )


def _configuration(args: argparse.Namespace) -> ServiceConfig:
    return ServiceConfig(
        database_path=Path(args.database),
        expected_sha256=args.sha256,
        expected_dataset_version=args.dataset_version,
        bind=args.bind,
        port=args.port,
    )


def _check_ready(port: int) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/readyz")
        response = connection.getresponse()
        response.read()
        return 0 if response.status == HTTPStatus.OK else 1
    except OSError:
        return 1
    finally:
        connection.close()


def _serve(config: ServiceConfig) -> NoReturn:
    server = create_server(config)
    status, payload = server.state.readiness()
    print(
        json.dumps(
            {
                "bind": config.bind,
                "port": server.server_port,
                "readiness": payload,
                "status_code": status,
            },
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    raise RuntimeError("server stopped unexpectedly")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serve the immutable OpenCEPGeo SQLite lookup contract."
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("OPENCEPGEO_DATABASE", _DEFAULT_DATABASE),
    )
    parser.add_argument(
        "--sha256",
        default=os.environ.get("OPENCEPGEO_DATABASE_SHA256", ""),
    )
    parser.add_argument(
        "--dataset-version",
        default=os.environ.get("OPENCEPGEO_DATASET_VERSION", ""),
    )
    parser.add_argument(
        "--bind",
        default=os.environ.get("OPENCEPGEO_BIND", _DEFAULT_BIND),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("OPENCEPGEO_PORT", str(_DEFAULT_PORT)),
    )
    parser.add_argument(
        "--check-ready",
        action="store_true",
        help="check the local readiness endpoint instead of starting the service",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("port must be in the range 1..65535", file=sys.stderr)
        return 2
    if args.check_ready:
        return _check_ready(args.port)
    _serve(_configuration(args))


if __name__ == "__main__":
    raise SystemExit(main())
