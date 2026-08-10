import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
MODULE_PATH = ROOT / "version_info.py"
EXPECTED_VERSION = (REPOSITORY / "VERSION").read_text(encoding="utf-8").strip()


def load_version_info():
    spec = importlib.util.spec_from_file_location("version_info_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_version_file_is_the_platform_version_source(monkeypatch, tmp_path):
    module = load_version_info()
    version_file = tmp_path / "VERSION"
    version_file.write_text(EXPECTED_VERSION + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "_version_candidates", lambda: (version_file,))
    module.get_platform_version.cache_clear()

    assert module.get_platform_version() == EXPECTED_VERSION
    assert module.get_version_info()["config_schema_supported"] == 1


def test_missing_version_file_returns_unknown(monkeypatch, tmp_path):
    module = load_version_info()
    monkeypatch.setattr(module, "_version_candidates", lambda: (tmp_path / "missing",))
    module.get_platform_version.cache_clear()

    assert module.get_platform_version() == "unknown"


def test_platform_git_commit_environment_has_priority(monkeypatch):
    module = load_version_info()
    monkeypatch.setenv("PLATFORM_GIT_COMMIT", "release-commit")
    module.get_git_commit.cache_clear()

    assert module.get_git_commit() == "release-commit"


def test_missing_git_repository_and_command_are_non_fatal(monkeypatch, tmp_path):
    module = load_version_info()
    module_dir = tmp_path / "librenms+grafana"
    module_dir.mkdir()
    monkeypatch.delenv("PLATFORM_GIT_COMMIT", raising=False)
    monkeypatch.setattr(module, "MODULE_DIR", module_dir)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    module.get_git_commit.cache_clear()

    assert module.get_git_commit() == "unknown"

    (module_dir.parent / ".git").mkdir()
    module.get_git_commit.cache_clear()
    assert module.get_git_commit() == "unknown"


def test_repository_version_uses_yyyy_mm_patch_format():
    value = (REPOSITORY / "VERSION").read_text(encoding="utf-8").strip()

    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", value)
