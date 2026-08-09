from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from service_helpers import write_service_database


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def create_internal_network(name: str) -> None:
    subnet_value = os.environ.get("OPENCEPGEO_TEST_SUBNET", "").strip()
    args = ["docker", "network", "create", "--internal"]
    if subnet_value:
        subnet = ipaddress.ip_network(subnet_value, strict=True)
        private_ranges = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        if not isinstance(subnet, ipaddress.IPv4Network) or not any(
            subnet.subnet_of(private_range) for private_range in private_ranges
        ):
            raise ValueError("OPENCEPGEO_TEST_SUBNET must be an RFC1918 IPv4 subnet")
        args.extend(("--subnet", str(subnet)))
    run(*args, name)


def main() -> int:
    suffix = uuid.uuid4().hex[:12]
    image = f"opencepgeo-pin188-smoke:{suffix}"
    network = f"opencepgeo-pin188-{suffix}"
    container = f"opencepgeo-pin188-{suffix}"
    consumer = f"opencepgeo-pin188-consumer-{suffix}"
    created_network = False
    created_container = False
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "opencepgeo.sqlite"
            sha256 = write_service_database(database)
            database.chmod(0o444)
            run("docker", "build", "--pull", "--tag", image, ".")
            create_internal_network(network)
            created_network = True
            run(
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--network",
                network,
                "--network-alias",
                "opencepgeo",
                "--mount",
                f"type=bind,source={database},target=/data/opencepgeo.sqlite,readonly",
                "--env",
                f"OPENCEPGEO_DATABASE_SHA256={sha256}",
                "--env",
                "OPENCEPGEO_DATASET_VERSION=fixture-service-v1",
                image,
            )
            created_container = True
            for _attempt in range(30):
                check = run(
                    "docker",
                    "exec",
                    container,
                    "python",
                    "-m",
                    "opencepgeo.service",
                    "--check-ready",
                    check=False,
                )
                if check.returncode == 0:
                    break
                time.sleep(1)
            else:
                logs = run("docker", "logs", container, check=False)
                raise AssertionError(f"container never became ready:\n{logs.stdout}\n{logs.stderr}")

            inspect = json.loads(run("docker", "inspect", container).stdout)[0]
            assert inspect["Config"]["User"] == "65532:65532"
            assert inspect["Config"]["Healthcheck"]["Test"][-1] == "--check-ready"
            assert inspect["HostConfig"]["ReadonlyRootfs"] is True
            assert "ALL" in inspect["HostConfig"]["CapDrop"]
            assert not inspect["HostConfig"]["PortBindings"]
            assert not inspect["NetworkSettings"]["Ports"]
            assert any(
                option.startswith("no-new-privileges")
                for option in inspect["HostConfig"]["SecurityOpt"]
            )
            assert inspect["Mounts"][0]["RW"] is False
            network_inspect = json.loads(
                run("docker", "network", "inspect", network).stdout
            )[0]
            assert network_inspect["Internal"] is True

            routes = run(
                "docker",
                "exec",
                container,
                "python",
                "-c",
                "from pathlib import Path; "
                "rows=Path('/proc/net/route').read_text().splitlines()[1:]; "
                "assert not any(row.split()[1] == '00000000' "
                "for row in rows if row.split()), rows",
            )
            assert routes.returncode == 0

            baked_datasets = run(
                "docker",
                "exec",
                container,
                "python",
                "-c",
                "from pathlib import Path; "
                "print([str(p) for p in Path('/app').rglob('*.sqlite')])",
            )
            assert baked_datasets.stdout.strip() == "[]"

            response = run(
                "docker",
                "exec",
                container,
                "python",
                "-c",
                "import json,urllib.request; "
                "print(json.dumps(json.load(urllib.request.urlopen("
                "'http://127.0.0.1:8080/v1/cep/01001000'))))",
            )
            payload = json.loads(response.stdout)
            assert payload["status"] == "resolved"
            assert payload["data"]["cep"] == "01001000"

            prefix_response = run(
                "docker",
                "exec",
                container,
                "python",
                "-c",
                "import json,urllib.request; "
                "print(json.dumps(json.load(urllib.request.urlopen("
                "'http://127.0.0.1:8080/v1/cep/01001003/prefix'))))",
            )
            prefix_payload = json.loads(prefix_response.stdout)
            assert prefix_payload["status"] == "resolved"
            assert prefix_payload["data"]["member_ceps"] == [
                "01001000", "01001001", "01001002", "01001003",
            ]

            sibling_response = run(
                "docker",
                "run",
                "--rm",
                "--name",
                consumer,
                "--network",
                network,
                "--entrypoint",
                "python",
                image,
                "-c",
                "import json,urllib.request; "
                "payload=json.load(urllib.request.urlopen("
                "'http://opencepgeo:8080/readyz', timeout=5)); "
                "assert payload['status'] == 'ready'; print(json.dumps(payload))",
            )
            assert json.loads(sibling_response.stdout)["status"] == "ready"
    finally:
        run("docker", "rm", "--force", consumer, check=False)
        if created_container:
            run("docker", "rm", "--force", container, check=False)
        if created_network:
            run("docker", "network", "rm", network, check=False)
        run("docker", "image", "rm", "--force", image, check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
