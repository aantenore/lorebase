"""The GitHub workflows keep the release gates they document (docs/release.md).

The files are parsed, not run: a release is checked by CI before anything is published, the
images are built for every published platform and signed on their multi-arch index, and no
checkout leaves the token on the runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
PLATFORMS = {"linux/amd64", "linux/arm64"}
IMAGES = {
    "ghcr.io/aantenore/lorebase",
    "ghcr.io/aantenore/lorebase-ml",
    "ghcr.io/aantenore/lorebase-web",
}
CHECKOUT = "actions/checkout@"


def _load(name: str) -> dict[str, Any]:
    document: dict[Any, Any] = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
    # YAML 1.1 reads the key `on` as the boolean true.
    if True in document:
        document["on"] = document.pop(True)
    return document


def _triggers(workflow: dict[str, Any]) -> set[str]:
    on = workflow["on"]
    return {on} if isinstance(on, str) else set(on)


def _needs(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def _checkouts(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_name, step.get("with", {}))
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(CHECKOUT)
    ]


def _platforms(job: dict[str, Any]) -> set[str]:
    return {entry["os"] for entry in job["strategy"]["matrix"]["platform"]}


RELEASE = _load("release.yaml")
CI = _load("ci.yaml")
JOBS = RELEASE["jobs"]


def test_release_runs_the_ci_workflow_on_the_tag_before_publishing() -> None:
    assert "workflow_call" in _triggers(CI)
    assert JOBS["ci"]["uses"] == "./.github/workflows/ci.yaml"
    assert "inputs.tag" in JOBS["ci"]["with"]["ref"]
    for publisher in ("image-platforms", "release"):
        assert "ci" in _needs(JOBS[publisher]), publisher
    assert "image-platforms" in _needs(JOBS["images"])
    assert "release" in _needs(JOBS["pypi"])


def test_ci_stays_on_demand_and_on_release_only() -> None:
    assert _triggers(CI) == {"workflow_call", "workflow_dispatch"}


def test_ci_aggregator_needs_every_other_job() -> None:
    jobs = CI["jobs"]
    assert _needs(jobs["ci"]) == set(jobs) - {"ci"}


def test_images_are_built_for_every_platform_and_joined_into_one_index() -> None:
    builds = JOBS["image-platforms"]
    assert _platforms(builds) == PLATFORMS
    arm = next(p for p in builds["strategy"]["matrix"]["platform"] if p["os"] == "linux/arm64")
    assert "arm" in arm["runner"], "arm64 is built on a native runner, not emulated"
    assert {image["repository"] for image in builds["strategy"]["matrix"]["image"]} == IMAGES
    [build] = [s for s in builds["steps"] if s.get("id") == "build"]
    assert build["with"]["platforms"] == "${{ matrix.platform.os }}"
    assert "push-by-digest=true" in build["with"]["outputs"]
    merge = JOBS["images"]
    assert {entry["image"] for entry in merge["strategy"]["matrix"]["include"]} == IMAGES
    assert set(merge["env"]["PLATFORMS"].split()) == PLATFORMS


def test_the_signature_and_the_sbom_are_on_the_index_digest() -> None:
    steps = JOBS["images"]["steps"]
    [index] = [s for s in steps if s.get("id") == "index"]
    assert "docker buildx imagetools create" in index["run"]
    [sign] = [s for s in steps if "cosign sign" in s.get("run", "")]
    assert sign["env"]["DIGEST"] == "${{ steps.index.outputs.digest }}"
    assert "cosign attest" in sign["run"]


def test_the_docker_check_builds_on_every_published_platform() -> None:
    job = _load("docker.yaml")["jobs"]["backend-image"]
    assert _platforms(job) == PLATFORMS
    for step in job["steps"]:
        if str(step.get("uses", "")).startswith("docker/build-push-action@"):
            assert step["with"]["platforms"] == "${{ matrix.platform.os }}", step["name"]


def test_the_docs_site_is_published_from_the_tag_after_the_release() -> None:
    docs = JOBS["docs"]
    assert docs["uses"] == "./.github/workflows/docs.yaml"
    assert _needs(docs) == {"release"}
    assert "inputs.tag" in docs["with"]["ref"]
    assert {"pages", "id-token"} <= {k for k, v in docs["permissions"].items() if v == "write"}
    assert "workflow_call" in _triggers(_load("docs.yaml"))


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yaml")), ids=lambda p: p.name)
def test_every_checkout_drops_the_token(path: Path) -> None:
    for job_name, options in _checkouts(_load(path.name)):
        assert options.get("persist-credentials") is False, f"{path.name}: {job_name}"


@pytest.mark.parametrize("name", ["ci.yaml", "docs.yaml", "sbom.yaml"])
def test_called_workflows_check_out_the_ref_they_are_given(name: str) -> None:
    workflow = _load(name)
    assert "workflow_call" in _triggers(workflow)
    for job_name, options in _checkouts(workflow):
        assert options.get("ref") == "${{ inputs.ref }}", f"{name}: {job_name}"
