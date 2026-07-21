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
    assert "python:3.13-slim" in compose
    assert "monitor-platform-api:local" in compose


def test_deploy_rebuilds_local_images_after_repository_updates():
    deploy = read("deploy.sh")

    assert "docker compose up -d --remove-orphans --build" in deploy
    # Restart each source-mounted service individually so one absent service
    # under set -e cannot fail a deploy whose stack already came up fine.
    assert "for service in bigscreen platform-api alertmanager-feishu-bridge" in deploy
    assert 'docker compose restart "$service" ||' in deploy


def test_feishu_ws_sidecar_is_profile_gated_and_optional():
    compose = read("docker-compose.yml")
    env = read(".env.example")
    apply = read("apply-env.sh")
    platform_dockerfile = read("docker/platform-api/Dockerfile")
    # The long-connection sidecar only runs behind the feishu profile, so a
    # deployment without a self-built app never starts it.
    assert "feishu-ws:" in compose
    assert 'profiles: ["feishu"]' in compose
    # Setting the app id auto-activates the profile so operators don't hand-edit
    # COMPOSE_PROFILES after pasting the secret.
    assert "FEISHU_APP_ID" in apply and "feishu" in apply
    # Console apply runs inside platform-api, so the sidecar must not require a
    # second local build context that only exists on the host filesystem.
    feishu_service = compose.split("  feishu-ws:", 1)[1].split("  player-targets:", 1)[0]
    assert "${PLATFORM_API_IMAGE:-monitor-platform-api:local}" in feishu_service
    assert "docker/feishu-ws" not in feishu_service
    assert "pull_policy: never" in feishu_service
    assert "lark-oapi==1.7.1" in platform_dockerfile
    # Confirmation must be documented as working without the app (console panel).
    assert "待删除设备" in env or "控制台" in env
    assert "FEISHU_APP_ID=" in env


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


def test_control_basic_section_only_contains_event_name_and_layout():
    app = read("bigscreen/app.js")
    basic = app.split("<h3>基础</h3>", 1)[1].split("</section>", 1)[0]

    assert 'configInput("event.name", "赛事名称"' in basic
    assert 'configInput("event.default_layout", "默认赛制"' in basic
    assert "event.security_mode" not in basic
    assert "event.public_base_url" not in basic
    assert "delete value.event.security_mode" in app
    assert "delete value.event.public_base_url" in app


def test_control_number_inputs_do_not_expose_or_react_to_wheel_spinners():
    app = read("bigscreen/app.js")
    css = read("bigscreen/platform.css")

    assert 'configForm.addEventListener("wheel"' in app
    assert 'input.type === "number"' in app
    assert "input.blur()" in app
    assert 'input[type="number"]::-webkit-inner-spin-button' in css
    assert "-webkit-appearance: none" in css
    assert "-moz-appearance: textfield" in css


def test_control_exposes_feishu_app_credentials_and_directional_isp_hint():
    app = read("bigscreen/app.js")

    assert 'configInput("alerts.feishu_app_id", "飞书应用 App ID"' in app
    assert 'configInput("alerts.feishu_app_secret", "飞书应用 App Secret"' in app
    assert 'configInput("alerts.feishu_chat_id", "告警群 Chat ID（可选）"' in app
    assert "下载/上传" in app
    assert "1000/100" in app


def test_feishu_bridge_does_not_create_librenms_transport():
    auto_config = read("librenms-auto-config.sh")

    assert "configure_feishu_transport" not in auto_config
    assert "INSERT INTO alert_transports" not in auto_config


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
    assert "--exclude='./images.tar.sha256'" in package
    assert '(cd "$OUT_DIR" && sha256_file images.tar)' in package
    assert "verify_image_archive" in installer
    assert "images.tar not found" in installer
    assert 'docker image inspect "$image"' in installer


def test_librenms_source_patch_checks_content_instead_of_fixed_line_numbers():
    entrypoint = read("entrypoint-librenms.sh")

    assert "rrd_echo_count" in entrypoint
    assert "55s/echo" not in entrypoint
    assert "82s/echo" not in entrypoint
