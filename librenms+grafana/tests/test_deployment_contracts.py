from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_release_images_are_pinned_and_defaults_are_consistent():
    compose = read("docker-compose.yml")
    example = read(".env.example")

    assert ":latest" not in compose
    assert "librenms/librenms:26.6.1" in compose
    assert "crazymax/rrdcached:1.9.0-r4" in compose
    assert "SNMP_COMMUNITY=global" in example
    assert "COMPOSE_PROFILES=\n" in example
    assert "SNMP_COMMUNITY:-public" not in compose


def test_named_volume_and_bind_mount_contract_is_not_mixed():
    compose = read("docker-compose.yml")

    assert "- prometheus-data:/prometheus" in compose
    assert "- grafana-data:/var/lib/grafana" in compose
    assert "./prometheus-data:/prometheus-data" not in compose
    assert "./grafana-data:/grafana-data" not in compose
    assert "  librenms-db-data:\n" not in compose
    assert "  librenms-data:\n" not in compose


def test_bigscreen_runtime_config_is_encoded_before_javascript_embedding():
    compose = read("docker-compose.yml")

    assert 'TITLE_B64="$$(b64 "$${BIGSCREEN_TITLE:-}")"' in compose
    assert 'title: decodeConfigValue("$$TITLE_B64")' in compose
    assert 'title: "$${BIGSCREEN_TITLE:-}"' not in compose


def test_apply_failure_does_not_mass_delete_services():
    script = read("apply-env.sh")

    assert "cleanup_conflicting_containers" not in script
    assert 'docker rm -f "$name"' not in script
    assert "PLATFORM_API_SELF_APPLY" in script


def test_offline_bundle_excludes_live_secrets_and_requires_integrity_check():
    package = read("offline-package.sh")
    installer = read("install-offline.sh")

    for excluded in ("./.git", "./.env", "./event-config.yml", "./platform-state"):
        assert f"--exclude='{excluded}'" in package
    assert "--profile '*' config --images" in package
    assert "images.tar.sha256" not in package  # generated generically by sha256_file
    assert "verify_image_archive" in installer
    assert "images.tar not found" in installer
    assert 'docker image inspect "$image"' in installer


def test_librenms_source_patch_checks_content_instead_of_fixed_line_numbers():
    entrypoint = read("entrypoint-librenms.sh")

    assert "rrd_echo_count" in entrypoint
    assert "55s/echo" not in entrypoint
    assert "82s/echo" not in entrypoint
