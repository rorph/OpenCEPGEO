from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerContractTests(unittest.TestCase):
    def test_image_is_data_agnostic_unprivileged_and_healthchecked(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "python:3.11-slim@sha256:"
            "db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93",
            dockerfile,
        )
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn('"--check-ready"', dockerfile)
        self.assertNotIn("EXPOSE", dockerfile)
        self.assertNotIn("COPY data", dockerfile)
        self.assertNotIn("COPY out", dockerfile)

    def test_dataset_artifacts_are_excluded_from_the_build_context(self):
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.sqlite", ignore)
        self.assertIn("data", ignore)
        self.assertIn("out", ignore)

    def test_documented_runtime_has_internal_subnet_and_no_host_port(self):
        runbook = (ROOT / "docs/service-runbook.md").read_text(encoding="utf-8")
        compose = runbook.split("```yaml", 1)[1].split("```", 1)[0]
        self.assertIn("internal: true", compose)
        self.assertIn("subnet: 10.215.0.0/24", compose)
        self.assertIn("name: px-cep-internal", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn(
            "image: opencepgeo:git-${OPENCEPGEO_SERVICE_COMMIT:?set full source commit}",
            compose,
        )
        self.assertIn(
            "/srv/opencepgeo/releases/2026.2.1-rc2/"
            "opencepgeo-2026.2.1-rc2.sqlite",
            compose,
        )
        self.assertNotIn("ports:", compose)
        self.assertNotIn("PIN-188", compose)
        self.assertIn("external: true", runbook)
        self.assertGreaterEqual(runbook.count("name: px-cep-internal"), 2)

        rollback = runbook.split("## Rollback", 1)[1]
        self.assertIn("service image commit tag or manifest digest", rollback)
        self.assertIn("artifact path, full SHA-256, and dataset version", rollback)

    def test_prefix_contract_is_bounded_and_excludes_municipality_coordinates(self):
        runbook = (ROOT / "docs/service-runbook.md").read_text(encoding="utf-8")
        self.assertIn("GET /v1/cep/{cep}/prefix", runbook)
        self.assertIn("at most 1,000 members", runbook)
        self.assertIn("capped at 16 KiB", runbook)
        self.assertIn("at least three", runbook)
        self.assertIn("10 km", runbook)
        self.assertIn("Municipality", runbook)
        self.assertIn('"member_ceps"', runbook)

    def test_actions_use_verified_immutable_revisions(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count(
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4"
        ), 2)
        self.assertEqual(workflow.count(
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5"
        ), 2)
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)

    def test_private_self_hosting_rights_decision_preserves_public_gate(self):
        notice = (ROOT / "NOTICE.md").read_text(encoding="utf-8")
        provenance = (ROOT / "docs/source-provenance.md").read_text(encoding="utf-8")
        for document in (notice, provenance):
            self.assertIn("private internal", document.lower())
            self.assertIn("self-hosting", document.lower())
            self.assertIn("public redistribution", document.lower())
            self.assertIn("OpenCEP", document)
            self.assertIn("IBGE", document)
            self.assertIn("OpenStreetMap", document)
            self.assertNotIn("Rod", document)
            self.assertNotIn("OpenCEP/DNE", document)

    def test_container_job_and_sibling_probe_have_bounded_timeouts(self):
        workflow = (ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        smoke = (ROOT / "tests/container_smoke.py").read_text(encoding="utf-8")
        container_job = workflow.split("  container:", 1)[1]
        self.assertIn("timeout-minutes: 15", container_job)
        self.assertIn(
            "'http://opencepgeo:8080/readyz', timeout=5",
            smoke,
        )

    def test_smoke_subnet_is_an_explicit_optional_override(self):
        smoke = (ROOT / "tests/container_smoke.py").read_text(encoding="utf-8")
        self.assertIn("OPENCEPGEO_TEST_SUBNET", smoke)
        self.assertNotIn("10.253", smoke)
        self.assertIn("http://opencepgeo:8080/readyz", smoke)
        self.assertIn("/proc/net/route", smoke)
        self.assertIn('run("docker", "rm", "--force", consumer', smoke)


if __name__ == "__main__":
    unittest.main()
