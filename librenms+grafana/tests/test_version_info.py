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


def version_paths(monkeypatch, tmp_path):
    module = load_version_info()
    module_dir = tmp_path / "repository" / "librenms+grafana"
    module_dir.mkdir(parents=True)
    monkeypatch.setattr(module, "MODULE_DIR", module_dir)
    monkeypatch.delenv("PLATFORM_VERSION_FILE", raising=False)
    module.get_platform_version.cache_clear()
    return module, module_dir.parent / "VERSION", module_dir / "VERSION"


def test_repository_version_is_the_default_platform_version_source(monkeypatch, tmp_path):
    module, repository_version, _ = version_paths(monkeypatch, tmp_path)
    repository_version.write_text(EXPECTED_VERSION + "\n", encoding="utf-8")

    assert module.get_platform_version() == EXPECTED_VERSION
    assert module.get_version_info()["config_schema_supported"] == 1


def test_module_version_cannot_override_repository_version(monkeypatch, tmp_path):
    module, repository_version, module_version = version_paths(monkeypatch, tmp_path)
    repository_version.write_text(EXPECTED_VERSION + "\n", encoding="utf-8")
    module_version.write_text("2025.01.9\n", encoding="utf-8")

    assert module.get_platform_version() == EXPECTED_VERSION


def test_configured_version_file_keeps_highest_priority(monkeypatch, tmp_path):
    module, repository_version, module_version = version_paths(monkeypatch, tmp_path)
    configured_version = tmp_path / "configured-version"
    configured_version.write_text("2030.12.3\n", encoding="utf-8")
    repository_version.write_text(EXPECTED_VERSION + "\n", encoding="utf-8")
    module_version.write_text("2025.01.9\n", encoding="utf-8")
    monkeypatch.setenv("PLATFORM_VERSION_FILE", str(configured_version))

    assert module.get_platform_version() == "2030.12.3"


def test_module_version_remains_fallback_when_repository_version_is_missing(monkeypatch, tmp_path):
    module, _, module_version = version_paths(monkeypatch, tmp_path)
    module_version.write_text("2025.01.9\n", encoding="utf-8")

    assert module.get_platform_version() == "2025.01.9"


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
