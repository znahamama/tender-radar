"""The GitHub deploy workflow (.github/workflows/deploy.yml) and its Terraform side
(infra/prod/deploy.tf). Static checks on the files: nothing here talks to GitHub or AWS."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    wf = yaml.safe_load((WORKFLOWS / name).read_text())
    # YAML 1.1 reads the bare key `on` as the boolean True.
    if True in wf:
        wf["on"] = wf.pop(True)
    return wf


@pytest.fixture(scope="module")
def deploy() -> dict:
    return _load("deploy.yml")


def _run(step: dict) -> str:
    """A step's script with backslash line continuations joined."""
    return step.get("run", "").replace("\\\n", " ")


# One AWS action per pattern, named by the function env var it targets.
_ACTIONS = re.compile(
    r'update-function-code --function-name "\$(?P<update>WORKER|API)_FUNCTION"'
    r'|wait function-updated-v2 --function-name "\$(?P<wait>WORKER|API)_FUNCTION"'
    r'|lambda invoke --function-name "\$(?P<invoke>WORKER|API)_FUNCTION"'
    r"(?P<payload>[^\n]*?--payload '[^']*')"
    r"|(?P<health>\$\{API_URL%/\}/api/health)"
)


def _actions(steps: list[dict]) -> list[tuple[str, ...]]:
    found = []
    for step in steps:
        for m in _ACTIONS.finditer(_run(step)):
            if m["update"]:
                found.append(("update", m["update"]))
            elif m["wait"]:
                found.append(("wait", m["wait"]))
            elif m["invoke"]:
                task = '"task":"migrate"' in m["payload"]
                found.append(("migrate" if task else "invoke", m["invoke"]))
            else:
                found.append(("health",))
    return found


def test_deploy_order_is_worker_then_migrate_then_api(deploy):
    """Worker code first (so migrate runs the new migrations), migrate synchronously, then
    the API, then health. The API never serves new code on an unmigrated schema."""
    steps = deploy["jobs"]["deploy"]["steps"]
    assert _actions(steps) == [
        ("update", "WORKER"), ("wait", "WORKER"),
        ("migrate", "WORKER"),
        ("update", "API"), ("wait", "API"),
        ("health",),
    ]
    assert deploy["env"]["WORKER_FUNCTION"] == "tender-radar-worker"
    assert deploy["env"]["API_FUNCTION"] == "tender-radar-api"
    # A failed step must stop the chain: nothing before the summary may run regardless.
    for step in steps[:-1]:
        assert "if" not in step and not step.get("continue-on-error"), step.get("name")
    assert steps[-1]["name"] == "summary" and steps[-1]["if"] == "always()"


def test_migrate_fails_on_function_error_or_not_ok(deploy):
    migrate = next(s for s in deploy["jobs"]["deploy"]["steps"] if s.get("id") == "migrate")
    run = _run(migrate)
    assert "AWS_MAX_ATTEMPTS=1 aws lambda invoke" in run  # never re-sent by the CLI
    assert "--cli-read-timeout 910" in run  # past the worker's 900 s timeout
    assert "jq -e '.FunctionError' invoke.json" in run
    assert "jq -e '.ok == true' migrate.json" in run


def test_deploy_job_settings(deploy):
    assert deploy["on"]["push"] == {"branches": ["main"]}
    assert "sha" in deploy["on"]["workflow_dispatch"]["inputs"]
    assert deploy["permissions"] == {"contents": "read"}

    ci = deploy["jobs"]["ci"]
    assert ci["uses"] == "./.github/workflows/ci.yml"
    assert ci["with"]["ref"] == "${{ needs.resolve.outputs.sha }}"

    job = deploy["jobs"]["deploy"]
    assert set(job["needs"]) == {"resolve", "ci"}
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert job["environment"] == "prod"  # the OIDC subject the role trusts
    assert job["concurrency"] == {"group": "deploy-prod", "cancel-in-progress": False}
    assert job["permissions"] == {"id-token": "write", "contents": "read"}


def test_ci_checks_the_sha_being_deployed():
    ci = _load("ci.yml")
    assert ci["on"]["workflow_call"]["inputs"]["ref"]["required"] is True
    assert {"secrets", "test", "terraform"} <= set(ci["jobs"])
    for name, job in ci["jobs"].items():
        checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ inputs.ref }}", name


@pytest.mark.parametrize("name", ["ci.yml", "deploy.yml"])
def test_every_action_is_pinned_by_full_sha(name):
    text = (WORKFLOWS / name).read_text()
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, re.MULTILINE)
    assert uses
    for ref in uses:
        if ref.startswith("./"):
            continue  # a workflow in this repo, at this commit
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_build_matches_build_push_script(deploy):
    """Same targets, platform and flags as infra/scripts/build_push.sh."""
    script = (ROOT / "infra" / "scripts" / "build_push.sh").read_text()
    build = next(s for s in deploy["jobs"]["deploy"]["steps"] if s.get("id") == "images")
    for flag in ("--platform linux/arm64", "--provenance=false", "--sbom=false",
                 '--target "$target"', '--build-arg "GIT_SHA=${SHA}"', "for target in api worker"):
        assert flag in _run(build), flag
        assert flag.replace("${SHA}", "${sha}") in script.replace("\\\n", " "), flag


# --- Terraform --------------------------------------------------------------------------

def test_deploy_role_trusts_only_prod_environment_of_this_repo():
    tf = (ROOT / "infra" / "prod" / "deploy.tf").read_text()
    assert 'github_deploy_subject = "repo:znahamama/tender-radar:environment:prod"' in tf
    assert tf.count('test     = "StringEquals"') == 2  # aud and sub, no StringLike wildcard
    assert "StringLike" not in tf
    assert "max_session_duration = 3600" in tf


def test_ci_owns_the_lambda_images():
    lam = (ROOT / "infra" / "prod" / "lambda.tf").read_text()
    assert lam.count('resource "aws_lambda_function"') == 2
    assert lam.count("ignore_changes = [image_uri]") == 2
