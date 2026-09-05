import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path,ignored", [
    ("librenms+grafana/event-config.yml", True),
    ("librenms+grafana/event-config.yml.bak.20260905", True),
    ("librenms+grafana/event-config.yml.before-batch2", True),
    ("librenms+grafana/event-config.example.yml", False),
    ("librenms+grafana/event-config.template.yml", False),
    ("librenms+grafana/event-config.company.example.yml", False),
    ("other/event-config.yml", False),
    ("other/event-config.yml.bak.20260905", False),
    ("other/event-config.yml.before-batch2", False),
    ("librenms+grafana/event-config.yml.bak", False),
] + [(path.relative_to(ROOT).as_posix(), False)
     for path in (ROOT / "librenms+grafana/tests/fixtures/config").rglob("*")
     if path.is_file()])
def test_precise_local_event_config_ignore(tmp_path, path, ignored):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    def git(*args):
        return subprocess.run(["git", "-c", f"core.excludesFile={os.devnull}", *args],
                              cwd=tmp_path, env=env, capture_output=True, text=True)
    assert git("init", "--quiet").returncode == 0
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    result = git("check-ignore", "--no-index", "--", path)
    assert result.returncode in (0, 1), result.stderr
    assert (result.returncode == 0) is ignored
