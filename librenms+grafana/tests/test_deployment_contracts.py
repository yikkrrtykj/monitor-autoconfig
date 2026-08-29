import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_deploy_entrypoint_is_executable_in_git():
    tracked = subprocess.run(
        ["git", "ls-files", "-s", "--", "librenms+grafana/deploy.sh"],
        cwd=ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert tracked.split()[0] == "100755"


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

    # Tournament-switch discovery must produce visible progress instead of
    # buffering every line until the full run ends.
    assert "python3 -u /generate-player-targets.py 2>&1" in compose
    assert 'output="$$(python3 /generate-player-targets.py' not in compose
    # Quiet live clients are prompted before the stage MAC table is read.
    assert 'PLAYER_REFRESH_FDB: "${PLAYER_REFRESH_FDB:-true}"' in compose
    assert "PLAYER_REFRESH_FDB=true" in example
    assert 'PROMETHEUS_URL: "http://prometheus:9090"' in compose
    assert 'PLAYER_TARGET_HISTORY_LOOKBACK: "${PLAYER_TARGET_HISTORY_LOOKBACK:-24h}"' in compose
    assert "PLAYER_TARGET_HISTORY_LOOKBACK=24h" in example
    assert 'PLAYER_SWITCH_FULL_SCAN_INTERVAL: "${PLAYER_SWITCH_FULL_SCAN_INTERVAL:-21600}"' in compose
    assert "PLAYER_SWITCH_FULL_SCAN_INTERVAL=21600" in example
    player_service = compose.split("  player-targets:", 1)[1].split("  topology-collector:", 1)[0]
    assert "./target_utils.py:/target_utils.py:ro" in player_service
    assert 'EVENT_NAME: "${EVENT_NAME:-}"' in player_service
    assert "for key in EVENT_NAME TOURNAMENT_SWITCHES" in player_service
    assert "SWITCH_DISCOVERY_RANGE" not in player_service
    assert 'export PLAYER_SWITCH_FORCE_FULL_SCAN=true' in compose


def test_sysname_changes_are_confirmed_before_notification():
    compose = read("docker-compose.yml")
    example = read(".env.example")

    assert 'SYSNAME_CHANGE_CONFIRM_POLLS: "${SYSNAME_CHANGE_CONFIRM_POLLS:-2}"' in compose
    assert "SYSNAME_CHANGE_CONFIRM_POLLS=2" in example


def test_ap_ping_uses_controller_heartbeat_without_switch_polling():
    compose = read("docker-compose.yml")
    example = read(".env.example")
    bridge = read("alertmanager-feishu-bridge.py")

    assert (
        'UNIFI_AP_CONTROLLER_LAST_SEEN_MAX_AGE_SECONDS: '
        '"${UNIFI_AP_CONTROLLER_LAST_SEEN_MAX_AGE_SECONDS:-30}"'
    ) in compose
    assert "UNIFI_AP_CONTROLLER_LAST_SEEN_MAX_AGE_SECONDS=30" in example
    assert 'device.get("last_seen")' in bridge
    assert "_unifi_controller_heartbeat_fresh" in bridge


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


def test_alert_bridge_mounts_its_split_runtime_modules():
    compose = read("docker-compose.yml")
    bridge_service = compose.split("  alertmanager-feishu-bridge:", 1)[1].split("  librenms:", 1)[0]

    assert "./alertmanager-feishu-bridge.py:/app/bridge.py:ro" in bridge_service
    assert "./bridge_isp_watcher.py:/app/bridge_isp_watcher.py:ro" in bridge_service
    assert "./bridge_online_identity.py:/app/bridge_online_identity.py:ro" in bridge_service
    assert "./bridge_resource_watcher.py:/app/bridge_resource_watcher.py:ro" in bridge_service
    assert "./bridge_sysname_watcher.py:/app/bridge_sysname_watcher.py:ro" in bridge_service
    assert "./feishu_delivery.py:/app/feishu_delivery.py:ro" in bridge_service
    assert "./librenms_client.py:/app/librenms_client.py:ro" in bridge_service
    assert "./network_syslog.py:/app/network_syslog.py:ro" in bridge_service
    assert 'LIBRENMS_API_TIMEOUT: "${LIBRENMS_API_TIMEOUT:-5}"' in bridge_service


def test_alert_bridge_delegates_sysname_watching_to_its_split_runtime_module():
    bridge = read("alertmanager-feishu-bridge.py")
    watcher = read("bridge_sysname_watcher.py")

    assert "from bridge_sysname_watcher import SysnameChangeWatcher" in bridge
    assert "_SYSNAME_WATCHER = SysnameChangeWatcher(" in bridge
    assert "return _SYSNAME_WATCHER.run()" in bridge
    assert 'start_watcher("sysname-change", sysname_change_watcher)' in bridge
    assert "SYSNAME_CHANGE_ALERT_ENABLED =" in bridge
    assert "SYSNAME_CHANGE_POLL_INTERVAL =" in bridge
    assert "SYSNAME_CHANGE_CONFIRM_POLLS =" in bridge
    assert "SYSNAME_STATE_FILE =" in bridge
    assert "pending_changes = {}" not in bridge
    assert "pending_changes = {}" in watcher


def test_alert_bridge_delegates_online_identity_to_its_split_runtime_module():
    bridge = read("alertmanager-feishu-bridge.py")
    service = read("bridge_online_identity.py")

    assert "from bridge_online_identity import OnlineIdentityService" in bridge
    assert "_ONLINE_IDENTITY = OnlineIdentityService(" in bridge
    assert "state_file=DEVICE_ONLINE_STATE_FILE" in bridge
    assert "return _ONLINE_IDENTITY.mark_notified(*values)" in bridge
    assert "return _ONLINE_IDENTITY.migrate(primary, *legacy_values)" in bridge
    assert "return _ONLINE_IDENTITY.send_once(card, *identity_values)" in bridge
    assert "return _ONLINE_IDENTITY.send_new_lifecycle(card, *identity_values)" in bridge
    assert "DEVICE_ONLINE_STATE_LOCK" not in bridge
    assert "DEVICE_ONLINE_INFLIGHT" not in bridge
    assert "_load_json_set(DEVICE_ONLINE_STATE_FILE)" not in bridge
    assert "_save_json_set(DEVICE_ONLINE_STATE_FILE" not in bridge
    assert "_ONLINE_IDENTITY.known_identities()" in bridge
    assert "import threading" in service
    assert "import alertmanager" not in service
    assert "import feishu" not in service
    assert "import librenms" not in service


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
    config_editor = read("bigscreen/config/config-editor.js")
    config_model = read("bigscreen/config/config-model.js")
    basic = config_editor.split("<h3>基础</h3>", 1)[1].split("</section>", 1)[0]

    assert 'configInput("event.name", "赛事名称"' in basic
    assert 'configInput("event.default_layout", "默认赛制"' in basic
    assert "teamOrderConfigMarkup()" in basic
    assert "data-team-order-slot" in config_editor
    assert "data-team-order-reset" in config_editor
    assert "event.security_mode" not in basic
    assert "event.public_base_url" not in basic
    assert "delete value.event.security_mode" in config_model
    assert "delete value.event.public_base_url" in config_model


def test_bigscreen_config_model_is_loaded_before_app_and_owns_only_pure_helpers():
    app = read("bigscreen/app.js")
    config_editor = read("bigscreen/config/config-editor.js")
    config_model = read("bigscreen/config/config-model.js")
    index = read("bigscreen/index.html")

    assert "model: window.BSConfigModel" in app
    assert "} = model;" in config_editor
    for name in (
        "cloneControlConfig",
        "asConfigArray",
        "configScalar",
        "csvText",
        "splitConfigList",
        "controlConfigDefaults",
        "configPathGet",
        "configPathSet",
        "expandIpRangeText",
    ):
        assert f"function {name}(" in config_model
        assert f"function {name}(" not in app
        assert f"function {name}(" not in config_editor
    assert "document." not in config_model
    assert "fetch(" not in config_model
    assert "postPlatform" not in config_model
    assert "dirty" not in config_model
    assert "applyInProgress" not in config_model
    assert "config/config-model.js?v=20260827a" in index
    assert index.index("config/config-model.js?v=20260827a") < index.index("config/config-editor.js?v=20260827a")


def test_bigscreen_config_editor_is_loaded_before_app_and_owns_editor_state_and_dom():
    app = read("bigscreen/app.js")
    config_editor = read("bigscreen/config/config-editor.js")
    index = read("bigscreen/index.html")

    assert "function createConfigEditor(" in config_editor
    assert "return { bind, render, isApplyInProgress };" in config_editor
    assert "const configEditor = createConfigEditor({" in app
    assert "configEditor.render(snapshot.platformConfig, snapshot.dhcpSettings)" in app
    assert "configEditor.isApplyInProgress()" in app
    assert "configEditor.bind()" in app
    for token in (
        "function renderControlConfigForm(",
        "function collectControlConfigForm(",
        "function renderConfigEditor(",
        "function runConfigAction(",
        "dataset.telnetDirty",
        "configResultSticky",
        "controlConfigImportFile",
    ):
        assert token in config_editor
        assert token not in app
    assert "config/config-editor.js?v=20260827a" in index
    assert index.index("config/config-model.js?v=20260827a") < index.index("config/config-editor.js?v=20260827a")
    assert index.index("config/config-editor.js?v=20260827a") < index.index("app.js?v=20260828f")


def test_bigscreen_dhcp_model_is_loaded_before_app_and_owns_only_pure_helpers():
    app = read("bigscreen/app.js")
    dhcp_model = read("bigscreen/dhcp/dhcp-model.js")
    dhcp_panel = read("bigscreen/dhcp/dhcp-panel.js")
    index = read("bigscreen/index.html")

    assert "} = model;" in dhcp_panel
    for name in (
        "dhcpRangeAddresses",
        "compactDhcpAddresses",
        "dhcpPoolKey",
        "dhcpIpv4Number",
        "dhcpPoolMatchesSearch",
        "dhcpPoolMatchesFilter",
        "dhcpPoolSortValue",
        "compareDhcpPools",
        "buildDhcpAddressContext",
        "dhcpAddressState",
    ):
        assert f"function {name}(" in dhcp_model
        assert f"function {name}(" not in app
        assert f"function {name}(" not in dhcp_panel
    for token in (
        "document.",
        "querySelector",
        "fetch(",
        "BSApi",
        "setTimeout",
        "setInterval",
        "activePageId",
        "dhcpTimer",
        "dhcpSelectedPoolKey",
        "dhcpPoolSearchText",
        "dhcpPoolFilterValue",
    ):
        assert token not in dhcp_model
    assert "dhcp/dhcp-model.js?v=20260827a" in index
    assert index.index("dhcp/dhcp-model.js?v=20260827a") < index.index("dhcp/dhcp-panel.js?v=20260827a")


def test_bigscreen_dhcp_panel_owns_page_state_dom_api_and_refresh_lifecycle():
    app = read("bigscreen/app.js")
    panel = read("bigscreen/dhcp/dhcp-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createDhcpPanel } = window.BSDhcpPanel;" in app
    assert "const dhcpPanel = createDhcpPanel({" in app
    assert "return { start, stop, hasScheduledRefresh };" in panel
    assert "dhcpPanel.start()" in app
    assert "dhcpPanel.stop()" in app
    assert "dhcpPanel.hasScheduledRefresh()" in app
    for token in (
        "dhcpTimer",
        "dhcpSeq",
        "dhcpRefreshing",
        "dhcpHasData",
        "dhcpLastPayload",
        "dhcpBindingPayload",
        "dhcpBindingsRefreshing",
        "dhcpSelectedPoolKey",
        "dhcpPoolSearchText",
        "dhcpPoolFilterValue",
    ):
        assert token in panel
        assert token not in app
    for name in (
        "dhcpSummaryCard",
        "dhcpPoolCard",
        "renderDhcpPoolBrowser",
        "dhcpAddressMap",
        "renderDhcpDashboard",
        "refreshDhcpBindings",
        "scheduleDhcpRefresh",
        "refreshDhcpDashboard",
    ):
        assert f"function {name}(" in panel
        assert f"function {name}(" not in app
    for selector in (
        "dhcpStatus",
        "dhcpSummary",
        "dhcpPools",
        "dhcpRefresh",
        "dhcpBindings",
        "dhcpBindingsStatus",
        "dhcpPoolSearch",
        "dhcpPoolFilter",
        "dhcpPoolCount",
        "dhcpFootnote",
        "dhcpConnection",
    ):
        assert selector in panel
        assert selector not in app
    assert "fetchDhcpDashboard(force)" in panel
    assert "fetchDhcpBindings()" in panel
    assert "fetchDhcpDashboard(" not in app
    assert "fetchDhcpBindings(" not in app
    assert 'document.addEventListener("visibilitychange"' in panel
    assert 'document.addEventListener("visibilitychange"' not in app
    assert "Math.max(30, Number(seconds || 60)) * 1000" in panel
    assert "seq !== dhcpSeq" in panel
    assert "dhcp/dhcp-panel.js?v=20260827a" in index
    assert index.index("dhcp/dhcp-model.js?v=20260827a") < index.index("dhcp/dhcp-panel.js?v=20260827a")
    assert index.index("dhcp/dhcp-panel.js?v=20260827a") < index.index("app.js?v=20260828f")


def test_control_number_inputs_do_not_expose_or_react_to_wheel_spinners():
    config_editor = read("bigscreen/config/config-editor.js")
    css = read("bigscreen/platform.css")

    assert 'configForm.addEventListener("wheel"' in config_editor
    assert 'input.type === "number"' in config_editor
    assert "input.blur()" in config_editor
    assert 'input[type="number"]::-webkit-inner-spin-button' in css
    assert "-webkit-appearance: none" in css
    assert "-moz-appearance: textfield" in css


def test_switch_editor_reserves_a_full_column_for_management_ip():
    css = read("bigscreen/platform.css")
    block = css.split(
        '.config-list[data-config-list="stage_switches"] .config-list-row,', 1,
    )[1].split("}", 1)[0]

    assert 'data-config-list="access_switches"' in block
    assert (
        "grid-template-columns: minmax(118px, 0.85fr) "
        "minmax(138px, 1fr) 58px;"
    ) in block


def test_screen_title_links_back_to_home():
    html = read("bigscreen/index.html")
    css = read("bigscreen/platform.css")

    assert 'class="screen-title-link"' in html
    assert 'id="screenHomeLink" href="/" aria-label="返回首页"' in html
    assert ".screen-title-link" in css


def test_all_bigscreen_pages_have_mobile_layout_contracts():
    app = read("bigscreen/app.js")
    wireless_panel = read("bigscreen/wireless/wireless-panel.js")
    css = read("bigscreen/platform.css")
    html = read("bigscreen/index.html")

    assert "@media (max-width: 960px)" in css
    assert ".screen.tournament-mode .tournament-panel" in css
    assert ".screen.tournament-mode .panel-grid" in css
    assert ".match-board" in css
    assert ".evidence-panel" in css
    assert ".incident-panel" in css
    assert ".topology-panel" in css
    assert ".wireless-table-row span::before" in css
    assert ".control-panel" in css
    assert ".dhcp-toolbar .dhcp-actions" in css
    assert 'data-label="IP"' in wireless_panel
    assert 'window.scrollTo({ top: 0, left: 0, behavior: "auto" })' in app
    assert "platform.css?v=20260803b" in html
    assert "app.js?v=20260828f" in html


def test_control_exposes_feishu_app_credentials_and_directional_isp_hint():
    config_editor = read("bigscreen/config/config-editor.js")

    assert 'configInput("alerts.feishu_app_id", "飞书应用 App ID"' in config_editor
    assert 'configInput("alerts.feishu_app_secret", "飞书应用 App Secret"' in config_editor
    assert 'configInput("alerts.feishu_chat_id", "告警及巡检群名称或 Chat ID"' in config_editor
    assert "下载/上传" in config_editor
    assert "1000/100" in config_editor

    ws = read("feishu-ws-client.py")
    assert "poll_site_group_commands" in ws
    assert "/open-apis/im/v1/messages?" in ws
    assert 'f"{BRIDGE_URL}/bot/query"' in ws

    compose = read("docker-compose.yml")
    feishu_service = compose.split("  feishu-ws:", 1)[1].split("  player-targets:", 1)[0]
    assert 'FEISHU_CHAT_ID: "${FEISHU_CHAT_ID:-}"' in feishu_service
    assert 'EVENT_NAME: "${EVENT_NAME:-}"' in feishu_service


def test_retired_isp_history_is_filtered_by_current_prometheus_targets():
    controller = read("bigscreen/infra/infra-controller.js")
    assert "infraCurrentTargets" in controller
    assert "fetchTopologyTargets()" in controller
    assert "!infraCurrentTargets.has(name)" in controller


def test_loss_heatmap_splits_large_device_lists_into_two_columns():
    app = read("bigscreen/app.js")
    infra_controller = read("bigscreen/infra/infra-controller.js")
    heatmap = read("bigscreen/charts/loss-heatmap.js")
    index = read("bigscreen/index.html")
    css = read("bigscreen/style.css")

    assert "const { createLossHeatmapRenderer } = window.BSLossHeatmap;" in app
    assert "const renderLossHeatmap = createLossHeatmapRenderer({" in app
    assert 'renderLossHeatmap("lossHeatmap", activeLossSeries)' in infra_controller
    assert 'renderLossHeatmap("lossHeatmap", [])' in infra_controller
    assert 'renderNoData(document.getElementById("lossHeatmap"))' not in app
    assert "function renderHeatmap(" not in app
    assert "const splitColumns = series.length > 12" in heatmap
    assert "series.slice(0, splitAt)" in heatmap
    assert "series.slice(splitAt)" in heatmap
    assert "const bucketCount = 60" in heatmap
    assert 'point.v > 0.5 ? "bad" : point.v > 0.01 ? "warn" : "good"' in heatmap
    assert "charts/loss-heatmap.js?v=20260826a" in index
    assert index.index("charts/loss-heatmap.js?v=20260826a") < index.index("app.js?v=20260828f")
    assert ".heatmap.heatmap-split" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert ".heatmap-axis-times > span" in css
    assert "white-space: nowrap" in css


def test_isp_and_evidence_use_business_specific_line_chart_facades():
    app = read("bigscreen/app.js")
    infra_controller = read("bigscreen/infra/infra-controller.js")
    isp_chart = read("bigscreen/charts/isp-chart.js")
    evidence_chart = read("bigscreen/charts/evidence-chart.js")
    evidence_panel = read("bigscreen/evidence/evidence-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createIspChartRenderer } = window.BSIspChart;" in app
    assert "const { createEvidenceChartRenderer } = window.BSEvidenceChart;" in app
    assert "const { createEvidencePanel } = window.BSEvidencePanel;" in app
    assert "const renderIspChart = createIspChartRenderer({" in app
    assert "const renderEvidenceCharts = createEvidenceChartRenderer({" in app
    assert "const evidencePanel = createEvidencePanel({" in app
    assert "renderLineChart(" not in app
    assert "renderIspChart({" in infra_controller
    assert "renderEvidenceCharts({" not in app
    assert "renderEvidenceCharts({" in evidence_panel
    assert "createIspCarousel" not in isp_chart
    assert "ispChartMaxBps(result.name, resultIndex)" in isp_chart
    assert 'calcs: ["last", "max"]' in isp_chart
    assert "renderEvidenceSummary(summaryContainerId" in evidence_chart
    assert "const latencyGap = Math.max(5, estimateStepSeconds(latencySeries) * 3)" in evidence_chart
    assert "const successGap = Math.max(5, estimateStepSeconds(successSeries) * 3)" in evidence_chart
    assert 'calcs: ["last", "min"]' in evidence_chart
    assert 'color: "#73d17a"' in evidence_chart
    assert "charts/isp-chart.js?v=20260826a" in index
    assert "charts/evidence-chart.js?v=20260826a" in index
    assert "evidence/evidence-panel.js?v=20260827a" in index
    assert index.index("charts/line-chart.js?v=20260826a") < index.index("charts/isp-chart.js?v=20260826a")
    assert index.index("charts/line-chart.js?v=20260826a") < index.index("charts/evidence-chart.js?v=20260826a")
    assert index.index("charts/isp-chart.js?v=20260826a") < index.index("app.js?v=20260828f")
    assert index.index("charts/evidence-chart.js?v=20260826a") < index.index("evidence/evidence-panel.js?v=20260827a")
    assert index.index("evidence/evidence-panel.js?v=20260827a") < index.index("app.js?v=20260828f")


def test_incident_panel_owns_form_queries_rendering_and_request_lifecycle():
    app = read("bigscreen/app.js")
    incident = read("bigscreen/incident.js")
    panel = read("bigscreen/incident/incident-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createIncidentPanel } = window.BSIncidentPanel;" in app
    assert "const incidentPanel = createIncidentPanel({" in app
    assert "analyzeIncident: window.BSIncident.analyzeIncident" in app
    assert "incidentPanel.start()" in app
    assert "incidentPanel.stop()" in app
    assert "return { start, stop };" in panel
    assert "return active && generation === lifecycleGeneration;" in panel
    assert "const result = analyzeIncident(data, threshold);" in panel
    assert 'window.history.replaceState({}, "", `/incident?${params.toString()}`)' in panel
    assert 'probe_icmp_duration_seconds{role="player",network="wired",phase="rtt"}' in panel
    assert 'probe_success{role="player",network="wired"}' in panel
    assert "function analyzeIncident(" in incident
    assert "document." not in incident
    for token in (
        "function incidentWindow(",
        "function queryIncidentData(",
        "function renderIncidentVerdict(",
        "function renderIncidentPlayers(",
        "function renderIncidentInfra(",
        "function renderIncidentIsp(",
        "function renderIncidentStage(",
        "function runIncidentAnalysis(",
        'document.getElementById("incidentAt")',
        'document.getElementById("incidentThreshold")',
        'document.getElementById("incidentVerdict")',
    ):
        assert token in panel
        assert token not in app
    assert "function setupIncidentPanel(" not in app
    assert "function readUrlIntoForm(" in panel
    assert "function bind(" in panel
    assert "incident.js?v=20260803a" in index
    assert "incident/incident-panel.js?v=20260827a" in index
    assert index.index("incident.js?v=20260803a") < index.index("incident/incident-panel.js?v=20260827a")
    assert index.index("incident/incident-panel.js?v=20260827a") < index.index("app.js?v=20260828f")


def test_wireless_panel_is_loaded_after_players_and_owns_page_controller():
    app = read("bigscreen/app.js")
    panel = read("bigscreen/wireless/wireless-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createWirelessPanel } = window.BSWirelessPanel;" in app
    assert "const wirelessPanel = createWirelessPanel({" in app
    assert "wirelessPanel.start(page)" in app
    assert "wirelessPanel.stop()" in app
    assert "wirelessPanel.hasScheduledRefresh()" in app
    assert "return { start, stop, hasScheduledRefresh };" in panel
    for token in (
        "let wirelessTimer = null;",
        "function renderWirelessKpis(",
        "function renderWirelessControls(",
        "function renderWirelessBoard(",
        "function fetchApStatus(",
        "function renderApStrip(",
        "function refreshWirelessOverview(",
        'document.getElementById("wirelessSummary")',
        'document.getElementById("wirelessBoard")',
        'document.getElementById("wirelessRescan")',
        'fetchPlayerSnapshot(\'role="player",network="wireless"\')',
        'unpoller_device_info{type="uap"}',
    ):
        assert token in panel
        assert token not in app
    assert "function showWireless(page)" in app
    assert "triggerRescan," in app
    assert "players.js?v=20260802a" in index
    assert "wireless/wireless-panel.js?v=20260827a" in index
    assert index.index("players.js?v=20260802a") < index.index("wireless/wireless-panel.js?v=20260827a")
    assert index.index("wireless/wireless-panel.js?v=20260827a") < index.index("app.js?v=20260828f")


def test_tournament_panel_is_loaded_after_players_and_owns_page_controller():
    app = read("bigscreen/app.js")
    panel = read("bigscreen/tournament/tournament-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createTournamentPanel } = window.BSTournamentPanel;" in app
    assert "const tournamentPanel = createTournamentPanel({" in app
    assert "tournamentPanel.start(page)" in app
    assert "tournamentPanel.stop()" in app
    assert "tournamentPanel.refresh(current)" in app
    assert "tournamentPanel.hasScheduledRefresh()" in app
    assert "return { start, stop, refresh, hasScheduledRefresh };" in panel
    for token in (
        "let tournamentTimer = null;",
        "let tournamentSeq = 0;",
        "function configuredTournamentPage(",
        "function renderSparkline(",
        "function renderTournamentSummary(",
        "function renderTournamentBoard(",
        "function tournamentTrendQuery(",
        "function renderTournamentTrend(",
        "async function refresh(",
        'document.getElementById("tournamentSummary")',
        'document.getElementById("tournamentBoard")',
        'document.getElementById("tournamentTrendChart")',
        'document.getElementById("tournamentRefresh")',
        'avg by (team,seat) (probe_icmp_duration_seconds{${selector},phase="rtt"})',
        "window.setInterval(() => refresh(page), 5000)",
    ):
        assert token in panel
        assert token not in app
    for shared_token in (
        "function teamName(",
        "function tournamentSelector(",
        "function latencyUrlForPlayer(",
        "async function fetchPlayerSnapshot(",
    ):
        assert shared_token in app
        assert shared_token not in panel
    assert "function showTournament(page)" in app
    assert "infraController.start();" in app
    assert "infraController.enterTournamentMode();" in app
    assert "startInfraRefresh" not in app
    assert "ispCarousel" not in panel
    assert "controlPanel" not in panel
    assert "players.js?v=20260802a" in index
    assert "tournament/tournament-panel.js?v=20260828a" in index
    assert index.index("players.js?v=20260802a") < index.index("tournament/tournament-panel.js?v=20260828a")
    assert index.index("tournament/tournament-panel.js?v=20260828a") < index.index("app.js?v=20260828f")


def test_infra_controller_owns_refresh_rendering_and_isp_lifecycle():
    app = read("bigscreen/app.js")
    controller = read("bigscreen/infra/infra-controller.js")
    index = read("bigscreen/index.html")

    assert "const { createInfraController } = window.BSInfraController;" in app
    assert "const infraController = createInfraController({" in app
    assert "infraController.enterInfraMode();" in app
    assert "infraController.enterTournamentMode();" in app
    assert "infraController.start();" in app
    assert "infraController.stop();" in app
    assert "infraController.hasScheduledRefresh()" in app
    assert "infraController.refreshForResize();" in app
    assert "return { createInfraController };" in controller
    assert "return {\n      start,\n      stop,\n      enterInfraMode,\n      enterTournamentMode,\n      hasScheduledRefresh,\n      refreshForResize\n    };" in controller
    for token in (
        "let gaugeTimer = null;",
        "let chartTimer = null;",
        "let seenUpTimer = null;",
        "let infraSeenUp = null;",
        "let infraCurrentTargets = null;",
        "let gaugeSeq = 0;",
        "let chartSeq = 0;",
        "let stageDeviceRegexCache = null;",
        "let ispTrafficResults = [];",
        "function stageDevicePattern()",
        "function renderGaugeGrid(",
        "function renderIspPanels(",
        "async function refreshInfraSeenUp()",
        "async function refreshGauges()",
        "async function refreshCharts()",
        "const ispCarousel = createIspCarousel({",
        "window.setInterval(refreshGauges, 5000)",
        "window.setInterval(refreshCharts, 5000)",
        "window.setInterval(refreshInfraSeenUp, 30000)",
        "prometheusRangeCached(pingTrendQuery, metricName, 2)",
        "prometheusRangeCached(pingSuccessTrendQuery, metricName, 2)",
        "buildInfrastructurePingPresentation({",
    ):
        assert token in controller
        assert token not in app
    for shared_token in (
        "function activePage()",
        "function shouldRender(",
        "const renderSignatures = new Map();",
        "function renderNoData(",
        "function setVisible(",
        "function activeInfraPingQuery",
    ):
        if shared_token == "function activeInfraPingQuery":
            assert "activeInfraPingQuery," in app
        else:
            assert shared_token in app
        assert shared_token not in controller
    assert "isStageFilterActive: () => Boolean(activePage().kind)" in app
    assert "onDataSuccess: () => { lastDataSuccessAt = Date.now(); }" in app
    assert "clearRenderSignatures: () => renderSignatures.clear()" in app
    assert "infra/infra-controller.js?v=20260828a" in index
    assert index.index("isp-carousel.js?v=20260731a") < index.index("infra/infra-controller.js?v=20260828a")
    assert index.index("infra/infra-controller.js?v=20260828a") < index.index("app.js?v=20260828f")


def test_iperf_controller_is_loaded_after_pure_helpers_and_owns_browser_state_machine():
    app = read("bigscreen/app.js")
    controller = read("bigscreen/control/iperf-controller.js")
    delivery = read("bigscreen/control/delivery-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createIperfController } = window.BSIperfController;" in app
    assert "const iperfController = createIperfController({" in app
    assert "iperfController.ensureMounted(element)" in delivery
    assert "iperfController.ensureMounted(element)" not in app
    assert "return { ensureMounted };" in controller
    assert "start()" not in controller
    assert "stop()" not in controller
    for token in (
        "let iperfPresets",
        "let pendingIperfRequest",
        "let activeIperfTaskId",
        "let iperfProgressTimer",
        "let iperfProgressRefreshing",
        'id="iperfConfirm"',
        'id="iperfProgress"',
        'id="iperfHistory"',
        'const iperfTaskStorageKey = "bigscreen.iperfTaskId"',
        "window.setInterval(refreshIperfProgress, 500)",
        "error.status === 409 && runningTaskId",
        '/不存在|过期/.test(status.error || "")',
        'postPlatform("/network/iperf3/stop"',
    ):
        assert token in controller
        assert token not in app
    assert "control/iperf-controller.js?v=20260828a" in index
    assert index.index("iperf.js?v=20260803a") < index.index("control/iperf-controller.js?v=20260828a")
    assert index.index("control/iperf-controller.js?v=20260828a") < index.index("control/delivery-panel.js?v=20260828a")
    assert index.index("control/delivery-panel.js?v=20260828a") < index.index("app.js?v=20260828f")


def test_delivery_panel_owns_operator_actions_and_mounts_existing_iperf_controller_once():
    app = read("bigscreen/app.js")
    panel = read("bigscreen/control/delivery-panel.js")
    controller = read("bigscreen/control/iperf-controller.js")
    index = read("bigscreen/index.html")

    assert "const { createDeliveryPanel } = window.BSDeliveryPanel;" in app
    assert "const deliveryPanel = createDeliveryPanel({" in app
    assert "deliveryPanel.render();" in app
    assert "return { render };" in panel
    assert "start" not in panel
    assert "stop" not in panel
    for token in (
        'document.getElementById("controlDelivery")',
        'element.dataset.built === "1"',
        'id="preCheckBtn"',
        'id="testAlertBtn"',
        'id="retirePendingRefreshBtn"',
        'id="retirePendingList"',
        'postPlatform("/pre-check", {})',
        'postPlatform("/test-alert", {})',
        'postPlatform("/network/retire/resolve", {',
        "fetchRetirePending()",
        "iperfController.ensureMounted(element)",
    ):
        assert token in panel
        assert token not in app
    for controller_token in (
        "let pendingIperfRequest",
        "let activeIperfTaskId",
        "let iperfProgressTimer",
        "sessionStorage",
        "window.setInterval(refreshIperfProgress, 500)",
        "error.status === 409 && runningTaskId",
    ):
        assert controller_token in controller
        assert controller_token not in panel
    assert "control/delivery-panel.js?v=20260828a" in index
    assert index.index("control/iperf-controller.js?v=20260828a") < index.index("control/delivery-panel.js?v=20260828a")
    assert index.index("control/delivery-panel.js?v=20260828a") < index.index("control/auth-controller.js?v=20260828a")
    assert index.index("control/auth-controller.js?v=20260828a") < index.index("app.js?v=20260828f")


def test_auth_controller_owns_control_auth_ui_actions_and_reliable_status_cache():
    app = read("bigscreen/app.js")
    controller = read("bigscreen/control/auth-controller.js")
    index = read("bigscreen/index.html")

    assert "const { createAuthController } = window.BSAuthController;" in app
    assert "const authController = createAuthController({" in app
    assert "authController.bind();" in app
    assert "await authController.ensureAuthenticated()" in app
    assert "return { bind, ensureAuthenticated };" in controller
    assert "onAuthenticated: () => refreshControlPanel()" in app
    assert "onLoggedOut: () => { lastControlReport = null; }" in app
    for token in (
        "let lastControlAuth = null;",
        "function setAuthMessage(",
        "function renderAuth(",
        "async function ensureAuthenticated(",
        "async function submitLogin(",
        "async function submitPasswordChange(",
        "async function logout(",
        'document.getElementById("controlAuth")',
        'document.getElementById("controlShell")',
        'document.getElementById("controlLoginForm")',
        'document.getElementById("controlPasswordForm")',
        'document.getElementById("controlLogout")',
        "status && status.transient && lastControlAuth && lastControlAuth.authenticated",
        "loginForm.addEventListener(\"submit\", submitLogin)",
        "passwordForm.addEventListener(\"submit\", submitPasswordChange)",
        "logoutBtn.addEventListener(\"click\", logout)",
    ):
        assert token in controller
        assert token not in app
    for app_token in (
        "let controlTimer = null;",
        "async function refreshControlPanel(",
        "function setupControlPanel(",
        "function startControlRefresh(",
        "function stopControlRefresh(",
        'document.getElementById("controlRefresh")',
        'document.getElementById("controlRescan")',
        "configEditor.bind();",
        "incidentRegistry.bind();",
        "window.setInterval(refreshControlPanel, 10000)",
    ):
        assert app_token in app
        assert app_token not in controller
    assert "control/auth-controller.js?v=20260828a" in index
    assert index.index("control/auth-controller.js?v=20260828a") < index.index("app.js?v=20260828f")


def test_incident_registry_owns_control_record_rendering_and_write_actions():
    app = read("bigscreen/app.js")
    registry = read("bigscreen/control/incident-registry.js")
    index = read("bigscreen/index.html")

    assert "const { createIncidentRegistry } = window.BSIncidentRegistry;" in app
    assert "const incidentRegistry = createIncidentRegistry({" in app
    assert "getControlReport: () => lastControlReport" in app
    assert "incidentRegistry.render(snapshot.incidents);" in app
    assert "incidentRegistry.bind();" in app
    assert "return { bind, render };" in registry
    for token in (
        "let lastIncidents = [];",
        "function render(payload)",
        "async function createIncident()",
        'document.getElementById("controlIncidentList")',
        'document.getElementById("controlIncidentTitle")',
        'document.getElementById("controlIncidentCreate")',
        'postPlatform("/incidents",',
        "patchPlatform(`/incidents/${button.dataset.resolveIncident}`",
        'status: "resolved"',
        'eventType: "recovery"',
    ):
        assert token in registry
        assert token not in app
    for app_token in (
        "let lastControlReport = null;",
        "function renderControlIncidentFlow(snapshot)",
        "async function collectControlSnapshot()",
        "function renderControlPanel(snapshot)",
        "fetchIncidents(),",
    ):
        assert app_token in app
        assert app_token not in registry
    assert "control/incident-registry.js?v=20260828a" in index
    assert index.index("control/auth-controller.js?v=20260828a") < index.index("control/incident-registry.js?v=20260828a")
    assert index.index("control/incident-registry.js?v=20260828a") < index.index("app.js?v=20260828f")


def test_topology_browser_controller_is_extracted_without_owning_refresh_or_data_fetch():
    app = read("bigscreen/app.js")
    panel = read("bigscreen/charts/topology-panel.js")
    index = read("bigscreen/index.html")

    assert "const { createTopologyPanel } = window.BSTopologyPanel;" in app
    assert "const topologyPanel = createTopologyPanel({" in app
    assert "topologyPanel.isAvailable()" in app
    assert "topologyPanel.prepare(targets, edges)" in app
    assert "topologyPanel.render({ layout, width })" in app
    assert "topologyPanel.updateLatency(layout.nodes)" in app
    assert "topologyPanel.showError(error.message || \"\")" in app
    assert "topologyPanel.clearDetail()" in app
    assert "topologyPanel.resetView()" in app
    assert "function bindTopologyNodeEvents" not in app
    assert "function setupTopoPanZoom" not in app
    assert "function updateTopologyLatencyTexts" not in app
    assert 'getElementById("topologyCanvas")' not in app
    assert "function refreshTopology" in app
    assert "function topologySignature" in app
    assert "function startTopologyRefresh" in app
    assert "function stopTopologyRefresh" in app
    assert "fetchTopologyTargets()" not in panel
    assert "fetchTopologyEdges()" not in panel
    assert "prometheus" not in panel.lower()
    assert "topologyTimer" not in panel
    assert "topologySeq" not in panel
    assert "topologySignature" not in panel
    assert "shouldRender" not in panel
    assert "charts/topology-panel.js?v=20260826a" in index
    assert index.index("topology.js?v=20260809a") < index.index("charts/topology-panel.js?v=20260826a")
    assert index.index("charts/topology-panel.js?v=20260826a") < index.index("app.js?v=20260828f")


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


def test_bigscreen_ping_trend_uses_job_aware_rtt_presentation():
    api = read("bigscreen/api.js")
    app = read("bigscreen/app.js")
    infra_controller = read("bigscreen/infra/infra-controller.js")
    line_chart = read("bigscreen/charts/line-chart.js")
    ping_chart = read("bigscreen/charts/ping-chart.js")
    evidence_chart = read("bigscreen/charts/evidence-chart.js")
    evidence_panel = read("bigscreen/evidence/evidence-panel.js")
    ping_transform = read("bigscreen/metrics/ping-transform.js")
    pages = read("bigscreen/pages.js")
    index = read("bigscreen/index.html")
    platform_config = read("platform_config.py")
    env_example = read(".env.example")

    # Keep the raw 2-second source stable and retain job metadata so the adapter
    # can select management-RTT or latency presentation without IP heuristics.
    assert "const end = Math.floor(now / step) * step;" in api
    assert 'const cacheKey = `${query}|${win.step}`;' in api
    assert 'max by (instance, job) (probe_icmp_duration_seconds{job=~' in pages
    assert 'phase="rtt"})' in pages
    ping_trend = next(
        line for line in pages.splitlines()
        if line.strip().startswith("pingTrend:")
    )
    ping_success_trend = next(
        line for line in pages.splitlines()
        if line.strip().startswith("pingSuccessTrend:")
    )
    infrastructure_trend_jobs = "infra-core-ping|infra-dist-ping|infra-fw-ping"
    assert infrastructure_trend_jobs in ping_trend
    assert infrastructure_trend_jobs in ping_success_trend
    assert 'max by (instance, job) (probe_success{job=~' in ping_success_trend
    assert "infra-dist-ping" in ping_trend
    assert "infra-srv-ping" not in ping_trend
    assert "infra-srv-ping" not in ping_success_trend
    assert "max_over_time(probe_icmp_duration_seconds" not in pages
    assert "prometheusRangeCached(pingTrendQuery, metricName, 2)" in infra_controller
    assert "prometheusRangeCached(pingSuccessTrendQuery, metricName, 2)" in infra_controller
    assert "const pingSuccessTrendQuery = queries.pingSuccessTrend || \"\";" in infra_controller
    assert (
        "const activePingSuccessSeries = "
        "visibleInfraSeries(mergeInfraSeries(renameListWithInfraMap("
        "filterDeployed(pingSuccessSeries, (s) => s.name), nameMap), \"max\"));"
    ) in infra_controller
    assert "const { buildInfrastructurePingPresentation } = window.BSPingTransform;" in app
    assert "buildInfrastructurePingPresentation({" in infra_controller
    assert "latencySeries: rawActivePingSeries" in infra_controller
    assert "successSeries: activePingSuccessSeries" in infra_controller
    assert "buildInfrastructurePingPresentation(rawActivePingSeries)" not in infra_controller
    legacy_spike_helper = "suppressIsolated" + "LatencySpikes"
    assert legacy_spike_helper not in app
    assert legacy_spike_helper not in read("bigscreen/utils.js")
    assert "const ns = { buildInfrastructurePingPresentation };" in ping_transform
    assert '"infra-core-ping"' in ping_transform
    assert '"infra-dist-ping"' in ping_transform
    assert '"infra-fw-ping"' in ping_transform
    assert "192.168." not in ping_transform
    assert 'const MANAGEMENT_RTT_PRESENTATION_MODE = "management-rtt";' in ping_transform
    assert 'const LATENCY_PRESENTATION_MODE = "latency";' in ping_transform
    assert "windowSeconds: 30" in ping_transform
    assert "minimumWindowPoints: 3" in ping_transform
    assert "quantile: 0.2" in ping_transform
    assert "emaAlpha: 0.5" in ping_transform
    assert "group.jobs.add(job);" in ping_transform
    assert "Array.from(group.jobs).every" in ping_transform
    assert "managementRttPresentation(timestamp, rawValue)" in ping_transform
    assert "nearestRankQuantile" in ping_transform
    assert 'v: null, status: "warming"' in ping_transform
    assert 'v: null, status: "online"' not in ping_transform
    assert 'v: 0, status: "online"' not in ping_transform
    assert "presentationMode," in ping_transform
    assert 'firewall_ping = named_targets([firewall], "ip")' in platform_config
    assert '"FIREWALL_PING": firewall_ping' in platform_config
    assert '"FIREWALL_SNMP_TARGETS": firewall_snmp or firewall_ping' in platform_config
    assert "控制台会和 FIREWALL_SNMP_TARGETS 使用同一组 IP" in env_example
    assert "correctedPingSeries" not in app
    assert "activePingSeries" not in app
    assert "smoothLatencyJitter" not in app
    assert "nearby.sort" not in ping_transform
    assert "return previous.v" in ping_transform
    assert "infra-srv-ping" in pages
    assert "smoothNormalLatencyJitter" not in app
    assert "baselineWindowSeconds: 60" in ping_transform
    assert "minimumBaselinePoints: 6" in ping_transform
    assert "minimumThreshold: 0.008" in ping_transform
    assert "medianMultiplier: 3" in ping_transform
    assert "madScale: 1.4826" in ping_transform
    assert "madMultiplier: 6" in ping_transform
    assert "persistentRunSeconds: 4" in ping_transform
    assert "fallbackNominalStepSeconds: 2" in ping_transform
    assert "maximumCandidateGapSteps: 1.5" in ping_transform
    assert "smoothingWindowPoints: 3" in ping_transform
    assert "emaAlpha: 0.5" in ping_transform
    assert "rawValue > threshold" in ping_transform
    assert "timestamp - run.startT >= SUCCESS_AWARE_DISPLAY_POLICY.persistentRunSeconds" in ping_transform
    assert "highRun.forEach" not in ping_transform
    assert "resetPresentationState();" in ping_transform
    assert 'currentStatus = "unknown";' in ping_transform
    assert "threshold: 0.02" in ping_transform
    assert "minConsecutive: 2" in ping_transform
    assert "maxGapSeconds: 3" in ping_transform
    assert "replacementRadius: 5" in ping_transform
    assert "replacementWindowSeconds: 15" in ping_transform
    assert "const { createPingChartRenderer } = window.BSPingChart;" in app
    assert "const renderPingChart = createPingChartRenderer({" in app
    assert "renderPingChart({" in infra_controller
    assert 'containerId: "pingTrendChart"' in infra_controller
    assert "series: displayLatencySeries" in infra_controller
    assert "The adapter already applies trailing causal smoothing" in ping_chart
    assert "smooth: false" in ping_chart
    assert "smooth: true" not in ping_chart
    assert "const pingGap = Math.max(5, estimateStepSeconds(series) * 3)" in ping_chart
    assert 'shouldRender("pingTrendChart", seriesSignature(displayLatencySeries))' in infra_controller
    assert "breakGapSeconds: pingGap" in ping_chart
    assert "renderInfraTrendCards" not in app
    assert ".infra-trend-grid" not in read("bigscreen/platform.css")
    assert "minMax: 0.005" in ping_chart
    assert "maxRoundStep: 0.01" in ping_chart
    assert "roundUpToStep(rawMax, maxRoundStep)" in line_chart
    assert "const tournamentPingLegend = tournamentMode" in ping_chart
    assert '{ legend: "bottom", legendNamesOnly: true, calcs: [] }' in ping_chart
    assert "...tournamentPingLegend" in ping_chart
    assert ".screen.tournament-mode .trend-panel .bottom-legend.names-only-legend" in read("bigscreen/platform.css")

    # Seat-based evidence first resolves the current Prometheus target and
    # does not silently reuse a manual IP left over from an earlier route.
    assert "resolveEvidenceCurrentIps(team, seat, network)" in evidence_panel
    assert "return active && seq === evidenceSeq;" in evidence_panel
    assert 'document.getElementById("evidenceIp").value = ip || ""' in evidence_panel
    assert '"evidenceTeam", "evidenceSeat", "evidenceNetwork"' in evidence_panel
    assert "当前没有可查询的 IP" in evidence_panel
    assert "evidencePanel.start()" in app
    assert "evidencePanel.stop()" in app
    assert "function queryEvidence(" not in app
    assert "function setupEvidencePanel(" not in app
    assert 'document.getElementById("evidenceTeam")' not in app

    # The headline switch value is a responsive median, not the single lowest
    # sample in a minute (which becomes zero after one failed probe).
    ping_gauge = next(
        line.strip() for line in pages.splitlines()
        if line.strip().startswith("pingGauge:")
    )
    loss_query = next(
        line.strip() for line in pages.splitlines()
        if line.strip().startswith("loss:")
    )
    assert ping_gauge == (
        "pingGauge: 'avg by (instance, job) (quantile_over_time(0.5, "
        "probe_icmp_duration_seconds{job=~\"infra-core-ping|infra-dist-ping|infra-fw-ping\","
        "phase=\"rtt\"}[30s])) or avg by (instance, job) (quantile_over_time(0.5, "
        "probe_icmp_duration_seconds{job=~\"infra-isp-ping|infra-srv-ping\",phase=\"rtt\"}[30s]))',"
    )
    assert loss_query == (
        "loss: 'max by (instance, job, target_ip) (1 - avg_over_time("
        "probe_success{job=~\"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping\"}[30s]))'"
    )
    assert "min_over_time(probe_icmp_duration_seconds" not in pages
    assert pages.count("quantile_over_time(0.5, probe_icmp_duration_seconds") == 2
    assert pages.count("[30s]") == 3
    assert "pages.js?v=20260826a" in index
    assert "players.js?v=20260802a" in index
    assert "api.js?v=20260810a" in index
    assert "app.js?v=20260828f" in index
    assert "utils.js?v=20260825a" in index
    assert "charts/line-chart.js?v=20260826a" in index
    assert "charts/ping-chart.js?v=20260826a" in index
    assert "metrics/ping-transform.js?v=20260826b" in index
    assert index.index("utils.js?v=20260825a") < index.index("charts/line-chart.js?v=20260826a")
    assert index.index("charts/line-chart.js?v=20260826a") < index.index("charts/ping-chart.js?v=20260826a")
    assert index.index("charts/ping-chart.js?v=20260826a") < index.index("app.js?v=20260828f")
    assert index.index("metrics/ping-transform.js?v=20260826b") < index.index("app.js?v=20260828f")
    assert "step: true" in evidence_chart
    assert "breakGapSeconds" in evidence_chart
    assert 'if (player.ip) params.set("ip", player.ip)' in app


def test_bigscreen_line_chart_supports_explicit_failure_points():
    line_chart = read("bigscreen/charts/line-chart.js")
    utils = read("bigscreen/utils.js")

    assert 'point.status !== "failure"' in utils
    assert 'point.status !== "unknown"' in utils
    assert "Number.isFinite(point.v)" in utils
    assert "lineSeriesStats(item.values)" in line_chart
    assert "lineFailurePoints(item.values)" in line_chart
    assert 'class="chart-failure-marker"' in line_chart
    assert 'stroke:#ff4d66' in line_chart
    assert "${failureMarkers}" in line_chart
    assert "options.smooth" in line_chart


def test_bigscreen_ping_legend_uses_authoritative_series_status():
    app = read("bigscreen/app.js")
    line_chart = read("bigscreen/charts/line-chart.js")
    ping_chart = read("bigscreen/charts/ping-chart.js")
    utils = read("bigscreen/utils.js")
    css = read("bigscreen/style.css")
    index = read("bigscreen/index.html")

    renderer_sources = app + line_chart + ping_chart
    assert "const series = seriesList.filter(lineSeriesHasTimeline);" in line_chart
    assert "lineSeriesCurrentDisplay(item, stats)" in line_chart
    assert "currentStatusLegend: true" in ping_chart
    assert '(currentStatusLegend ? ["last", "max"] : ["mean", "max"])' in line_chart
    assert 'currentDisplay.currentStatus === "offline"' in line_chart
    assert '<span class="legend-current-status legend-status-offline">OFFLINE</span>' in line_chart
    assert 'const MANAGEMENT_REACHABILITY_MODE = "management-reachability";' not in renderer_sources
    assert "isManagementReachabilitySeries(item)" not in renderer_sources
    assert '{ currentStatus: "online", label: "ONLINE", value: null }' not in renderer_sources
    assert "splitOnlineStatusOnGaps" not in renderer_sources
    assert 'class="chart-reachability-line"' not in renderer_sources
    assert 'class="chart-reachability-separator"' not in renderer_sources
    assert "reachabilityLaneLayout" not in renderer_sources
    assert "axisPadTop: 48" not in renderer_sources
    assert "...Array.from(statsBySeries.values())" in line_chart
    assert "const segments = splitPointsOnGaps(item.values, options.breakGapSeconds);" in line_chart
    assert "const failureMarkerY = height - pad.bottom - 6;" in line_chart
    assert '.legend-current-status.legend-status-offline' in css
    assert "color: #ff4d66;" in css
    assert 'if (currentStatus === "offline")' in utils
    assert 'if (currentStatus === "unknown")' in utils
    assert 'label: "OFFLINE"' in utils
    assert 'label: "--"' in utils
    assert 'const currentStatus = item.currentStatus === undefined ? "" : `#${item.currentStatus}`;' in utils
    assert "style.css?v=20260825a" in index
    assert "utils.js?v=20260825a" in index
    assert "app.js?v=20260828f" in index


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
    infra_controller = read("bigscreen/infra/infra-controller.js")
    css = read("bigscreen/platform.css")
    index = read("bigscreen/index.html")

    assert "createIspCarousel" in app
    assert "intervalMs: 10000" in infra_controller
    assert 'screen.className = "screen infra-mode";' in app
    assert 'screen.className = `screen tournament-mode' in app
    assert '.screen.tournament-mode .isp-grid.isp-paged' in css
    assert "isp-carousel.js?v=20260731a" in index
    assert "platform.css?v=20260803b" in index


def test_topology_isp_discovery_can_read_librenms_interface_inventory():
    compose = read("docker-compose.yml")
    topology = compose.split("  topology-collector:", 1)[1].split("  bigscreen:", 1)[0]

    assert "./librenms-data:/librenms-data:ro" in topology
    assert 'LIBRENMS_URL: "http://librenms:8000"' in topology
    assert 'LIBRENMS_TOKEN_FILE: "/librenms-data/librenms-api-token"' in topology
    assert "./librenms_client.py:/librenms_client.py:ro" in topology
    assert 'LIBRENMS_API_TIMEOUT: "${LIBRENMS_API_TIMEOUT:-5}"' in topology
    assert 'ISP_DISCOVERY_SOURCE: "${ISP_DISCOVERY_SOURCE:-hybrid}"' in topology
    assert 'ISP_LIBRENMS_POLL_MAX_AGE_SECONDS: "${ISP_LIBRENMS_POLL_MAX_AGE_SECONDS:-600}"' in topology
    assert "ISP_GATEWAY_AUTO_DISCOVER ISP_DISCOVERY_SOURCE ISP_LIBRENMS_POLL_MAX_AGE_SECONDS" in topology
    assert 'TOPOLOGY_DATA_SOURCE: "${TOPOLOGY_DATA_SOURCE:-hybrid}"' in topology
    assert 'TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS: "${TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS:-600}"' in topology
    assert 'TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS: "${TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS:-28800}"' in topology
    assert 'TOPOLOGY_SERVER_ATTACHMENT_SOURCE: "${TOPOLOGY_SERVER_ATTACHMENT_SOURCE:-hybrid}"' in topology
    assert 'TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS: "${TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS:-900}"' in topology
    assert 'TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS: "${TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS:-900}"' in topology
    assert "TOPOLOGY_SERVER_ATTACHMENT_SOURCE TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS" in topology
    assert "./target_utils.py:/target_utils.py:ro" in topology
    assert 'TOPOLOGY_SNMP_TIMEOUT: "${TOPOLOGY_SNMP_TIMEOUT:-2}"' in topology
    assert 'TOPOLOGY_SNMP_RETRIES: "${TOPOLOGY_SNMP_RETRIES:-0}"' in topology
    assert 'TOPOLOGY_POLL_WORKERS: "${TOPOLOGY_POLL_WORKERS:-1}"' in topology
    assert 'TOPOLOGY_SNMP_DELAY_MS: "${TOPOLOGY_SNMP_DELAY_MS:-500}"' in topology


def test_large_ping_trend_keeps_every_switch_identifiable():
    line_chart = read("bigscreen/charts/line-chart.js")
    ping_chart = read("bigscreen/charts/ping-chart.js")
    css = read("bigscreen/style.css")
    index = read("bigscreen/index.html")

    assert "sortLegendByMax: true" in ping_chart
    assert "const legendSeries = options.sortLegendByMax" in line_chart
    assert 'series.length > 24' in line_chart
    assert '"ultra-series"' in line_chart
    assert ".compact-series .side-legend" in css
    assert ".ultra-series .side-legend" in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "style.css?v=20260825a" in index
    assert "app.js?v=20260828f" in index


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
    snmp = read("snmp.yml")

    assert "cat > /tmp/snmp-ifstack.yml" in compose
    assert "ifStackStatus" in compose
    assert "pagpGroupIfIndex" in compose
    assert "1.3.6.1.4.1.9.9.98.1.1.1.1.8" in compose
    assert "dot3adAggActorAdminKey" in compose
    assert "1.2.840.10006.300.43.1.1.1.1.6" in compose
    assert "dot3adAggPortActorAdminKey" in compose
    assert "1.2.840.10006.300.43.1.2.1.1.4" in compose
    assert "dot3adAggPortAttachedAggID" in compose
    assert "1.2.840.10006.300.43.1.2.1.1.13" in compose
    assert "ActorSystemID" not in compose
    assert "--config.file=/tmp/snmp-ifstack.yml" in compose
    assert "./lag_ownership.py:/app/lag_ownership.py:ro" in compose
    assert "./lag_ownership.py:/lag_ownership.py:ro" in compose
    assert (
        'write_snmp_job "infra-switch-ifmib"  "$INTERCONNECT_SNMP_TARGETS"     '
        '"if_mib, if_stack"'
    ) in prometheus
    assert prometheus.count('write_snmp_job "infra-switch-ifmib"') == 1
    # if_stack is the sole owner of this table. Keeping it in if_mib used to
    # walk the same table twice in one scrape (and on every firewall too).
    assert "      - ifStackTable" not in snmp
    assert "      - sysUpTime" not in snmp
    assert "retries: 0" in snmp
    assert "timeout: 2s" in snmp


def test_cisco_resource_alert_uses_small_low_frequency_snmp_module_and_console_thresholds():
    compose = read("docker-compose.yml")
    prometheus = read("prometheus-gen-config.sh")
    config_editor = read("bigscreen/config/config-editor.js")
    env = read(".env.example")

    assert "cisco_resources:" in compose
    assert "1.3.6.1.4.1.9.9.109.1.1.1.1.8" in compose
    assert "1.3.6.1.4.1.9.9.48.1.1.1.6" in compose
    assert '--config.file=/tmp/snmp-cisco-resources.yml' in compose
    assert 'write_snmp_job "infra-switch-resources"' in prometheus
    assert '"cisco_resources" "$SWITCH_RESOURCE_SCRAPE_INTERVAL"' in prometheus
    assert "SWITCH_RESOURCE_SCRAPE_INTERVAL=120s" in env
    assert 'configInput("alerts.cpu_alert_percent", "交换机 CPU 告警阈值（%）"' in config_editor
    assert 'configInput("alerts.memory_alert_percent", "交换机内存告警阈值（%）"' in config_editor


def test_poll_pressure_defaults_and_existing_env_are_migrated():
    compose = read("docker-compose.yml")
    example = read(".env.example")

    assert 'PLAYER_SWITCH_PROBE_WORKERS: "${PLAYER_SWITCH_PROBE_WORKERS:-8}"' in compose
    assert 'PLAYER_SNMP_DELAY_MS: "${PLAYER_SNMP_DELAY_MS:-100}"' in compose
    assert 'SWITCH_DISCOVERY_WORKERS: "${SWITCH_DISCOVERY_WORKERS:-8}"' in compose
    assert "PLAYER_SWITCH_PROBE_WORKERS=8" in example
    assert "PLAYER_SWITCH_FULL_SCAN_INTERVAL=21600" in example
    assert "PLAYER_SNMP_DELAY_MS=100" in example
    assert "SWITCH_DISCOVERY_WORKERS=8" in example
    assert "TOPOLOGY_POLL_WORKERS=1" in example
    assert "TOPOLOGY_SNMP_DELAY_MS=500" in example
    assert "TOPOLOGY_DATA_SOURCE=hybrid" in example
    assert "TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS=600" in example
    assert "TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS=28800" in example
    assert "TOPOLOGY_SERVER_ATTACHMENT_SOURCE=hybrid" in example
    assert "TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS=900" in example
    assert "TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS=900" in example
    assert "ISP_DISCOVERY_SOURCE=hybrid" in example
    assert "ISP_LIBRENMS_POLL_MAX_AGE_SECONDS=600" in example
    for script_name in ("deploy.sh", "apply-env.sh"):
        script = read(script_name)
        assert "migrate_env_default SWITCH_RESOURCE_SCRAPE_INTERVAL 60s 120s" in script
        assert "migrate_env_default SWITCH_DISCOVERY_WORKERS 32 8" in script
        assert "migrate_env_default PLAYER_SWITCH_PROBE_WORKERS 32 8" in script
        assert "migrate_env_default PLAYER_SWITCH_FULL_SCAN_INTERVAL 1800 21600" in script
        assert "migrate_env_default PLAYER_TARGETS_REFRESH_INTERVAL 300 60" in script
        assert "migrate_env_default TOPOLOGY_POLL_WORKERS 2 1" in script
        assert "migrate_env_default TOPOLOGY_SNMP_DELAY_MS 250 500" in script

def test_gateway_mac_flap_alert_is_configurable_and_enabled_by_default():
    compose = read("docker-compose.yml")
    config_editor = read("bigscreen/config/config-editor.js")
    env = read(".env.example")

    assert "native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback" in compose
    assert "SYSLOG_GATEWAY_MACS" in compose
    assert "SYSLOG_GATEWAY_UPLINK_PORTS" in compose
    assert "SYSLOG_MAC_FLAP_WINDOW_SECONDS" in compose
    assert "SYSLOG_MAC_FLAP_THRESHOLD" in compose
    assert 'configInput("alerts.gateway_macs", "关键网关 MAC（逗号分隔）"' in config_editor
    assert 'configInput("alerts.gateway_uplink_ports", "网关正常上联接口（逗号分隔）"' in config_editor
    assert 'configInput("alerts.mac_flap_window_seconds", "MAC 漂移统计窗口（秒）"' in config_editor
    assert 'configInput("alerts.mac_flap_threshold", "普通 MAC 告警次数"' in config_editor
    assert "SYSLOG_MAC_FLAP_WINDOW_SECONDS=60" in env
    assert "SYSLOG_MAC_FLAP_THRESHOLD=3" in env


def test_apply_failure_does_not_mass_delete_services():
    script = read("apply-env.sh")

    assert "cleanup_conflicting_containers" not in script
    assert 'docker rm -f "$name"' not in script
    assert "PLATFORM_API_SELF_APPLY" in script


def test_platform_version_and_config_schema_are_wired_into_runtime_and_console():
    compose = read("docker-compose.yml")
    deploy = read("deploy.sh")
    example = read("event-config.example.yml")
    api = read("bigscreen/api.js")
    app = read("bigscreen/app.js")

    assert (ROOT.parent / "VERSION").read_text(encoding="utf-8").strip()
    assert example.startswith("schema_version: 1\n")
    assert "../VERSION:/workspace/VERSION:ro" in compose
    assert 'PLATFORM_VERSION_FILE: "/workspace/VERSION"' in compose
    assert 'PLATFORM_GIT_COMMIT: "${PLATFORM_GIT_COMMIT:-unknown}"' in compose
    assert 'export PLATFORM_GIT_COMMIT="$platform_git_commit"' in deploy
    assert 'platformApi("/version"' in api
    assert '{ label: "平台版本"' in app
    assert '{ label: "Git Commit"' in app
    assert '{ label: "配置版本"' in app
    assert "保存或应用时升级" in app


def test_abandoned_offline_bundle_workflow_is_not_shipped():
    client = read("bigscreen/api.js")
    config_editor = read("bigscreen/config/config-editor.js")
    readme = (ROOT.parent / "README.md").read_text(encoding="utf-8")

    assert not (ROOT / "offline-package.sh").exists()
    assert not (ROOT / "install-offline.sh").exists()
    assert "fetchDeliveryManifest" not in client
    assert "offline-package.sh" not in readme
    assert "install-offline.sh" not in readme
    assert "VM/OVA" in readme
    assert "事故和部署清单" not in readme
    assert '/\\.zip$/i.test(file.name)' in config_editor
    assert 'text.slice(0, 2) === "PK"' in config_editor
    assert "不支持导入压缩包" in config_editor
    assert "请选择 event-config.yml 配置文件" in config_editor
    assert 'postPlatform("/config/validate"' in config_editor


def test_librenms_source_patch_checks_content_instead_of_fixed_line_numbers():
    entrypoint = read("entrypoint-librenms.sh")

    assert "rrd_echo_count" in entrypoint
    assert "55s/echo" not in entrypoint
    assert "82s/echo" not in entrypoint
