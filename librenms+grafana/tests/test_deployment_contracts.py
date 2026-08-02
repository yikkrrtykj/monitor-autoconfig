import json
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


def test_player_target_generator_streams_and_refreshes_stage_fdb():
    compose = read("docker-compose.yml")
    example = read(".env.example")

    # A discovery across the fallback management range must produce visible
    # progress instead of buffering every line until the full run ends.
    assert "python3 -u /generate-player-targets.py 2>&1" in compose
    assert 'output="$$(python3 /generate-player-targets.py' not in compose
    # Quiet live clients are prompted before the stage MAC table is read.
    assert 'PLAYER_REFRESH_FDB: "${PLAYER_REFRESH_FDB:-true}"' in compose
    assert "PLAYER_REFRESH_FDB=true" in example
    assert 'PROMETHEUS_URL: "http://prometheus:9090"' in compose
    assert 'PLAYER_TARGET_HISTORY_LOOKBACK: "${PLAYER_TARGET_HISTORY_LOOKBACK:-24h}"' in compose
    assert "PLAYER_TARGET_HISTORY_LOOKBACK=24h" in example
    assert 'PLAYER_SWITCH_FULL_SCAN_INTERVAL: "${PLAYER_SWITCH_FULL_SCAN_INTERVAL:-1800}"' in compose
    assert "PLAYER_SWITCH_FULL_SCAN_INTERVAL=1800" in example
    assert 'export PLAYER_SWITCH_FORCE_FULL_SCAN=true' in compose


def test_sysname_changes_are_confirmed_before_notification():
    compose = read("docker-compose.yml")
    example = read(".env.example")

    assert 'SYSNAME_CHANGE_CONFIRM_POLLS: "${SYSNAME_CHANGE_CONFIRM_POLLS:-2}"' in compose
    assert "SYSNAME_CHANGE_CONFIRM_POLLS=2" in example


def test_deploy_rebuilds_local_images_only_when_dockerfiles_change():
    deploy = read("deploy.sh")

    assert "docker compose up -d --remove-orphans --build" in deploy
    assert ".deploy-local-image.sha256" in deploy
    assert "Dockerfiles unchanged; skipping rebuild" in deploy
    assert "docker image inspect" in deploy
    # Restart each source-mounted service individually so one absent service
    # under set -e cannot fail a deploy whose stack already came up fine.
    assert "for service in bigscreen platform-api alertmanager-feishu-bridge feishu-ws" in deploy
    assert 'docker compose restart "$service" ||' in deploy
    assert "docker compose up -d --force-recreate --no-deps librenms-config" in deploy


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
    assert 'TEAM_ORDERS_B64="$$(b64 "$${BIGSCREEN_TEAM_ORDERS:-}")"' in compose
    assert 'teamOrders: decodeConfigValue("$$TEAM_ORDERS_B64")' in compose
    assert 'title: "$${BIGSCREEN_TITLE:-}"' not in compose


def test_control_basic_section_only_contains_event_name_and_layout():
    app = read("bigscreen/app.js")
    basic = app.split("<h3>基础</h3>", 1)[1].split("</section>", 1)[0]

    assert 'configInput("event.name", "赛事名称"' in basic
    assert 'configInput("event.default_layout", "默认赛制"' in basic
    assert "teamOrderConfigMarkup()" in basic
    assert "data-team-order-slot" in app
    assert "data-team-order-reset" in app
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


def test_screen_title_links_back_to_home():
    html = read("bigscreen/index.html")
    css = read("bigscreen/platform.css")

    assert 'class="screen-title-link"' in html
    assert 'id="screenHomeLink" href="/" aria-label="返回首页"' in html
    assert ".screen-title-link" in css


def test_all_bigscreen_pages_have_mobile_layout_contracts():
    app = read("bigscreen/app.js")
    css = read("bigscreen/platform.css")
    html = read("bigscreen/index.html")

    assert "@media (max-width: 960px)" in css
    assert ".screen.tournament-mode .tournament-panel" in css
    assert ".screen.tournament-mode .panel-grid" in css
    assert ".match-board" in css
    assert ".evidence-panel" in css
    assert ".incident-panel" in css
    assert ".topology-panel" in css
    assert ".ops-table-row span::before" in css
    assert ".control-panel" in css
    assert ".dhcp-toolbar .dhcp-actions" in css
    assert 'data-label="IP"' in app
    assert 'window.scrollTo({ top: 0, left: 0, behavior: "auto" })' in app
    assert "platform.css?v=20260802e" in html
    assert "app.js?v=20260802h" in html


def test_control_exposes_feishu_app_credentials_and_directional_isp_hint():
    app = read("bigscreen/app.js")

    assert 'configInput("alerts.feishu_app_id", "飞书应用 App ID"' in app
    assert 'configInput("alerts.feishu_app_secret", "飞书应用 App Secret"' in app
    assert 'configInput("alerts.feishu_chat_id", "本监控的告警及巡检群名称")' in app
    assert "下载/上传" in app
    assert "1000/100" in app

    ws = read("feishu-ws-client.py")
    assert "poll_site_group_commands" in ws
    assert "/open-apis/im/v1/messages?" in ws
    assert 'f"{BRIDGE_URL}/bot/query"' in ws

    compose = read("docker-compose.yml")
    feishu_service = compose.split("  feishu-ws:", 1)[1].split("  player-targets:", 1)[0]
    assert 'FEISHU_CHAT_ID: "${FEISHU_CHAT_ID:-}"' in feishu_service
    assert 'EVENT_NAME: "${EVENT_NAME:-}"' in feishu_service


def test_retired_isp_history_is_filtered_by_current_prometheus_targets():
    app = read("bigscreen/app.js")
    assert "infraCurrentTargets" in app
    assert "fetchTopologyTargets()" in app
    assert "!infraCurrentTargets.has(name)" in app


def test_loss_heatmap_splits_large_device_lists_into_two_columns():
    app = read("bigscreen/app.js")
    css = read("bigscreen/style.css")

    assert "const splitColumns = series.length > 12" in app
    assert "series.slice(0, splitAt)" in app
    assert "series.slice(splitAt)" in app
    assert ".heatmap.heatmap-split" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".heatmap-axis-times > span" in css
    assert "white-space: nowrap" in css


def test_grafana_device_names_survive_low_frequency_snmp_scrapes():
    dashboard = read("grafana-provisioning/dashboard-json/event-infra.json")

    # sysName is intentionally scraped every ten minutes. Keep its last value
    # available to Grafana so Ping and loss rows do not alternate between the
    # hostname and target IP during Prometheus' shorter lookback interval.
    # The instant Ping gauge still resolves legacy target names through the
    # slowly-scraped sysName metric. Range panels use the stable instance label
    # already attached by Prometheus and avoid a fragile range-vector join.
    assert dashboard.count("last_over_time(sysName") == 2
    assert "max by (target_ip,sysName) (sysName{" not in dashboard


def test_grafana_range_panels_request_time_series_data():
    dashboard = json.loads(read("grafana-provisioning/dashboard-json/event-infra.json"))

    for panel_id in (200, 300, 401):
        panel = next(item for item in dashboard["panels"] if item["id"] == panel_id)
        target = panel["targets"][0]
        assert target["format"] == "time_series"
        assert target["instant"] is False
        assert target["range"] is True

    uptime = next(item for item in dashboard["panels"] if item["id"] == 101)
    assert "last_over_time(sysUpTime" in uptime["targets"][0]["expr"]
    assert "group_left(snmp_name)" not in uptime["targets"][0]["expr"]


def test_librenms_home_removes_unconfigured_server_stats_widget():
    config = read("librenms-auto-config.sh")
    compose = read("docker-compose.yml")
    example = read(".env.example")

    assert "$key === 'server_stats'" in config
    assert "$addWidget('server-stats'" not in config
    assert "LIBRENMS_HOME_SERVER_STATS" not in compose
    assert "LIBRENMS_HOME_SERVER_STATS" not in example


def test_grafana_ping_trend_keeps_short_spikes_across_refresh_alignment():
    dashboard = json.loads(read("grafana-provisioning/dashboard-json/event-infra.json"))
    panel = next(item for item in dashboard["panels"] if item["id"] == 200)
    target = panel["targets"][0]

    # Grafana's automatic range-query step changes alignment on every refresh.
    # Read every raw 2-second probe through a fixed window so a one-sample spike
    # cannot appear or disappear just because the query timestamps moved.
    assert target["interval"] == "2s"
    assert target["expr"].count(
        'max_over_time(probe_icmp_duration_seconds{job=~'
    ) == 1
    assert "[10s]" in target["expr"]


def test_bigscreen_ping_trend_is_combined_and_filters_isolated_spikes():
    api = read("bigscreen/api.js")
    app = read("bigscreen/app.js")
    pages = read("bigscreen/pages.js")
    index = read("bigscreen/index.html")

    # Keep the raw 2-second source stable, but suppress isolated 50 ms samples
    # in both overview and tournament displays. Sustained incidents stay raw.
    assert "const end = Math.floor(now / step) * step;" in api
    assert 'max by (instance) (probe_icmp_duration_seconds{job=~' in pages
    assert 'phase="rtt"})' in pages
    assert "max_over_time(probe_icmp_duration_seconds" not in pages
    assert "prometheusRangeCached(pingTrendQuery, metricName, 2)" in app
    assert "suppressIsolatedLatencySpikes(rawActivePingSeries" in app
    assert "smoothNormalLatencyJitter" not in app
    assert "threshold: 0.05" in app
    assert "minConsecutive: 2" in app
    assert 'renderLineChart("pingTrendChart", activePingSeries' in app
    assert "const pingGap = Math.max(5, estimateStepSeconds(activePingSeries) * 3)" in app
    assert "breakGapSeconds: pingGap" in app
    assert "renderInfraTrendCards" not in app
    assert ".infra-trend-grid" not in read("bigscreen/platform.css")
    assert "minMax: 0.005" in app
    assert 'const tournamentPingLegend = document.querySelector(".screen.tournament-mode")' in app
    assert '{ legend: "bottom", legendNamesOnly: true, calcs: [] }' in app
    assert "...tournamentPingLegend" in app
    assert ".screen.tournament-mode .trend-panel .bottom-legend.names-only-legend" in read("bigscreen/platform.css")

    # Seat-based evidence first resolves the current Prometheus target and
    # does not silently reuse a manual IP left over from an earlier route.
    assert "resolveEvidenceCurrentIps(team, seat, network)" in app
    assert 'seq !== evidenceSeq || activePageId !== "evidence"' in app
    assert 'document.getElementById("evidenceIp").value = ip || ""' in app
    assert '"evidenceTeam", "evidenceSeat", "evidenceNetwork"' in app
    assert "当前没有可查询的 IP" in app

    # The headline switch value is a responsive median, not the single lowest
    # sample in a minute (which becomes zero after one failed probe).
    assert "min_over_time(probe_icmp_duration_seconds" not in pages
    assert pages.count("quantile_over_time(0.5, probe_icmp_duration_seconds") == 2
    assert pages.count("[30s]") == 3
    assert "pages.js?v=20260802a" in index
    assert "players.js?v=20260802a" in index
    assert "api.js?v=20260730d" in index
    assert "app.js?v=20260802h" in index
    assert "utils.js?v=20260802d" in index
    assert "step: true" in app
    assert "breakGapSeconds" in app
    assert 'if (player.ip) params.set("ip", player.ip)' in app


def test_player_targets_keep_recently_offline_seats_visible_for_five_minutes():
    compose = read("docker-compose.yml")
    example = read(".env.example")
    assert 'PLAYER_OFFLINE_GRACE_SECONDS: "${PLAYER_OFFLINE_GRACE_SECONDS:-300}"' in compose
    assert "PLAYER_VERIFY_PING PLAYER_OFFLINE_GRACE_SECONDS" in compose
    assert 'PLAYER_TARGETS_REFRESH_INTERVAL: "${PLAYER_TARGETS_REFRESH_INTERVAL:-60}"' in compose
    assert 'interval="$${PLAYER_TARGETS_REFRESH_INTERVAL:-60}"' in compose
    assert "PLAYER_TARGETS_REFRESH_INTERVAL=60" in example


def test_tournament_isp_carousel_is_isolated_from_normal_infrastructure_view():
    app = read("bigscreen/app.js")
    css = read("bigscreen/platform.css")
    index = read("bigscreen/index.html")

    assert "createIspCarousel" in app
    assert "intervalMs: 10000" in app
    assert 'screen.className = "screen infra-mode";' in app
    assert 'screen.className = `screen tournament-mode' in app
    assert '.screen.tournament-mode .isp-grid.isp-paged' in css
    assert "isp-carousel.js?v=20260731a" in index
    assert "platform.css?v=20260802e" in index


def test_topology_isp_discovery_can_read_librenms_interface_inventory():
    compose = read("docker-compose.yml")
    topology = compose.split("  topology-collector:", 1)[1].split("  bigscreen:", 1)[0]

    assert "./librenms-data:/librenms-data:ro" in topology
    assert 'LIBRENMS_URL: "http://librenms:8000"' in topology
    assert 'LIBRENMS_TOKEN_FILE: "/librenms-data/librenms-api-token"' in topology
    assert "./target_utils.py:/target_utils.py:ro" in topology
    assert 'TOPOLOGY_SNMP_TIMEOUT: "${TOPOLOGY_SNMP_TIMEOUT:-2}"' in topology
    assert 'TOPOLOGY_SNMP_RETRIES: "${TOPOLOGY_SNMP_RETRIES:-0}"' in topology
    assert 'TOPOLOGY_POLL_WORKERS: "${TOPOLOGY_POLL_WORKERS:-2}"' in topology
    assert 'TOPOLOGY_SNMP_DELAY_MS: "${TOPOLOGY_SNMP_DELAY_MS:-250}"' in topology


def test_feishu_bridge_does_not_create_librenms_transport():
    auto_config = read("librenms-auto-config.sh")

    assert "configure_feishu_transport" not in auto_config
    assert "INSERT INTO alert_transports" not in auto_config


def test_existing_librenms_devices_receive_current_snmp_credentials():
    auto_config = read("librenms-auto-config.sh")

    assert '"snmpver": os.environ["DEVICE_SNMPVER"]' in auto_config
    assert "sync_device_snmp_api" in auto_config
    assert 'curl -s -X PATCH "$LIBRENMS_URL/api/v0/devices/$ip"' in auto_config
    assert '"field": ["community", "snmpver", "port", "transport", "snmp_disable", "disabled"]' in auto_config


def test_existing_ping_only_targets_are_migrated_away_from_snmp():
    auto_config = read("librenms-auto-config.sh")

    assert "ping_device_payload" in auto_config
    assert '"snmp_disable": True' in auto_config
    assert '"display_template": name' in auto_config
    assert "sync_ping_device_api" in auto_config
    assert '"field": ["snmp_disable", "os", "sysName", "hardware", "display", "disabled"]' in auto_config
    assert '"data": [1, "ping", name, "ICMP", name, 0]' in auto_config
    assert 'if sync_ping_device_api "$name" "$ip"; then' in auto_config
    assert "existing device converted to ping-only" in auto_config


def test_stale_player_subnet_devices_are_retired_from_librenms_polling():
    compose = read("docker-compose.yml")
    auto_config = read("librenms-auto-config.sh")
    config_service = compose.split("  librenms-config:", 1)[1].split("  grafana:", 1)[0]

    assert 'PLAYER_SUBNETS: "${PLAYER_SUBNETS:-}"' in config_service
    assert 'PLAYER_GATEWAYS: "${PLAYER_GATEWAYS:-}"' in config_service
    assert "retire_unmanaged_player_devices" in auto_config
    assert "disable_librenms_device_api" in auto_config
    assert '{"field":["disabled"],"data":[1]}' in auto_config


def test_librenms_discovery_icmp_gates_snmp_checks():
    auto_config = read("librenms-auto-config.sh")

    assert 'fping -a -r 0 -t "$LIBRENMS_DISCOVERY_PING_TIMEOUT_MS"' in auto_config
    assert 'done < "$discovery_reachable"' in auto_config
    assert auto_config.index("ping_filter_targets \"$discovery_candidates\"") < auto_config.index(
        'while read -r ip; do\n  [ -z "$ip" ] && continue\n\n  # 不再按 IP'
    )


def test_cisco_stackwise_uses_dedicated_low_frequency_snmp_module():
    compose = read("docker-compose.yml")
    prometheus = read("prometheus-gen-config.sh")
    env = read(".env.example")

    assert "cisco_stackwise:" in compose
    assert "1.3.6.1.4.1.9.9.500.1.2.1.1.6" in compose
    assert '--config.file=/tmp/snmp-stackwise.yml' in compose
    assert "./discover-stackwise-targets.py:/discover-stackwise-targets.py:ro" in compose
    assert 'STACKWISE_TARGETS_FILE: "/targets/stackwise_targets.json"' in compose
    assert "python3 /discover-stackwise-targets.py" in compose
    assert (
        'write_snmp_job "infra-switch-stackwise" ""                           '
        '"cisco_stackwise" "$STACKWISE_SCRAPE_INTERVAL" "$STACKWISE_TARGETS_FILE"'
    ) in prometheus
    assert 'write_snmp_job "infra-switch-stackwise" "$SWITCH_SNMP_TARGETS"' not in prometheus
    assert 'SWITCH_IFMIB_SCRAPE_INTERVAL: "${SWITCH_IFMIB_SCRAPE_INTERVAL:-30s}"' in compose
    assert 'STACKWISE_SCRAPE_INTERVAL: "${STACKWISE_SCRAPE_INTERVAL:-60s}"' in compose
    assert "SWITCH_IFMIB_SCRAPE_INTERVAL=30s" in env
    assert "STACKWISE_SCRAPE_INTERVAL=60s" in env
    for script_name in ("deploy.sh", "apply-env.sh"):
        script = read(script_name)
        assert "migrate_env_default SWITCH_IFMIB_SCRAPE_INTERVAL 10s 30s" in script
        assert "migrate_env_default STACKWISE_SCRAPE_INTERVAL 30s 60s" in script
    assert "STACKWISE_DISCOVERY_TIMEOUT=1" in env


def test_interconnect_job_collects_port_channel_member_relationships():
    compose = read("docker-compose.yml")
    prometheus = read("prometheus-gen-config.sh")

    assert "cat > /tmp/snmp-ifstack.yml" in compose
    assert "ifStackStatus" in compose
    assert "--config.file=/tmp/snmp-ifstack.yml" in compose
    assert (
        'write_snmp_job "infra-switch-ifmib"  "$INTERCONNECT_SNMP_TARGETS"     '
        '"if_mib, if_stack"'
    ) in prometheus


def test_cisco_resource_alert_uses_small_low_frequency_snmp_module_and_console_thresholds():
    compose = read("docker-compose.yml")
    prometheus = read("prometheus-gen-config.sh")
    app = read("bigscreen/app.js")
    env = read(".env.example")

    assert "cisco_resources:" in compose
    assert "1.3.6.1.4.1.9.9.109.1.1.1.1.8" in compose
    assert "1.3.6.1.4.1.9.9.48.1.1.1.6" in compose
    assert '--config.file=/tmp/snmp-cisco-resources.yml' in compose
    assert 'write_snmp_job "infra-switch-resources"' in prometheus
    assert '"cisco_resources" "$SWITCH_RESOURCE_SCRAPE_INTERVAL"' in prometheus
    assert "SWITCH_RESOURCE_SCRAPE_INTERVAL=60s" in env
    assert 'configInput("alerts.cpu_alert_percent", "交换机 CPU 告警阈值（%）"' in app
    assert 'configInput("alerts.memory_alert_percent", "交换机内存告警阈值（%）"' in app


def test_gateway_mac_flap_alert_is_configurable_and_enabled_by_default():
    compose = read("docker-compose.yml")
    app = read("bigscreen/app.js")
    env = read(".env.example")

    assert "native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback" in compose
    assert "SYSLOG_GATEWAY_MACS" in compose
    assert "SYSLOG_GATEWAY_UPLINK_PORTS" in compose
    assert "SYSLOG_MAC_FLAP_WINDOW_SECONDS" in compose
    assert "SYSLOG_MAC_FLAP_THRESHOLD" in compose
    assert 'configInput("alerts.gateway_macs", "关键网关 MAC（逗号分隔）"' in app
    assert 'configInput("alerts.gateway_uplink_ports", "网关正常上联接口（逗号分隔）"' in app
    assert 'configInput("alerts.mac_flap_window_seconds", "MAC 漂移统计窗口（秒）"' in app
    assert 'configInput("alerts.mac_flap_threshold", "普通 MAC 告警次数"' in app
    assert "SYSLOG_MAC_FLAP_WINDOW_SECONDS=60" in env
    assert "SYSLOG_MAC_FLAP_THRESHOLD=3" in env


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
