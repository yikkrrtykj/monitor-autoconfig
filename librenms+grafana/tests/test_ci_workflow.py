import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def read_workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_ci_runs_for_main_pushes_and_pull_requests_on_python_313():
    workflow = read_workflow()

    assert "push:\n    branches: [main]" in workflow
    assert "pull_request:\n    branches: [main]" in workflow
    assert 'python-version: "3.13"' in workflow
    assert 'python-version: "3.11"' not in workflow
    assert "${{ secrets." not in workflow


def test_ci_runs_the_required_repository_checks():
    workflow = read_workflow()

    assert "pip install --disable-pip-version-check -r librenms+grafana/requirements-dev.txt" in workflow
    assert "run: pytest -q" in workflow
    assert "python -m compileall -q -f librenms+grafana" in workflow
    assert "shellcheck --severity=error" in workflow
    assert 'bash -n "$script"' in workflow
    assert 'sh -n "$script"' in workflow
    assert "node --check" in workflow
    assert "docker compose config --quiet" in workflow


def test_compose_check_uses_temporary_example_configuration_without_starting_images():
    workflow = read_workflow()

    assert "cp .env.example .env" in workflow
    assert "cp event-config.example.yml event-config.yml" in workflow
    assert "rm -f .env event-config.yml" in workflow
    assert not re.search(r"docker compose (?:build|create|pull|run|start|up)\\b", workflow)


def test_existing_javascript_smoke_and_dashboard_checks_are_retained():
    workflow = read_workflow()

    assert 'node "$file"' in workflow
    assert "scripts/smoke-test.sh --static" in workflow
    assert "grafana-provisioning/dashboard-json/*.json" in workflow
