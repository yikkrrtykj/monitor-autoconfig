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
    assert "shellcheck --severity=warning" in workflow
    assert "shellcheck --severity=error" not in workflow
    assert 'bash -n "$script"' in workflow
    assert 'sh -n "$script"' in workflow
    assert "node --check" in workflow
    assert "docker compose config --quiet" in workflow


def test_compose_check_uses_temporary_example_configuration_without_starting_images():
    workflow = read_workflow()

    assert "cp .env.example .env" in workflow
    assert "cp event-config.example.yml event-config.yml" in workflow
    assert "rm -f .env event-config.yml" in workflow
    assert not re.search(r"docker compose (?:up|start)\b", workflow)


def test_ci_has_isolated_topology_collector_import_smoke():
    workflow = read_workflow()

    assert "Isolated topology-collector import smoke" in workflow
    assert 'project="p0-ap-placement-import-${GITHUB_RUN_ID}-${GITHUB_JOB}"' in workflow
    assert 'docker compose -p "$project" build topology-collector' in workflow
    assert "run --rm --no-deps" in workflow
    assert "--entrypoint python3 topology-collector" in workflow
    assert 'runpy.run_path("/generate-topology-edges.py", run_name="topology_import_only")' in workflow
    assert "down -v --remove-orphans" in workflow
    assert "trap cleanup EXIT" in workflow
    # Import-only must never start the collector entrypoint or a production stack.
    assert "python3 /generate-topology-edges.py" not in workflow.split(
        "Isolated topology-collector import smoke", 1)[1]
    assert not re.search(r"docker compose (?:up|start)\b", workflow)


def test_existing_javascript_smoke_and_dashboard_checks_are_retained():
    workflow = read_workflow()

    assert 'node "$file"' in workflow
    assert "scripts/smoke-test.sh --static" in workflow
    assert "grafana-provisioning/dashboard-json/*.json" in workflow
