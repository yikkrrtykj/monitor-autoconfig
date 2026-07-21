import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platform_config.py"
spec = importlib.util.spec_from_file_location("platform_config", MODULE_PATH)
platform_config = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(platform_config)


SAMPLE = """
event:
  name: Test Event
  mode: match
  default_layout: tournament-64-2layer
networks:
  player_vlan: 40
  player_subnets: 192.168.40.0/24
  wireless_subnets: 192.168.41.0/24
  player_gateways: 192.168.10.254
devices:
  core:
    name: core
    ip: 192.168.10.254
  firewall:
    name: firewall
    ip: 192.168.10.1
  switches:
    - name: stage-1
      ip: 192.168.10.11
      role: access
    - name: stage-2
      ip: 192.168.10.12
      role: access
isp:
  auto_discovery: false
  max_bandwidth_mbps: 2000
  links:
    - name: telecom
      ping: 223.5.5.5
      ip: 203.0.113.10
alerts:
  mode: match
security:
  grafana_anonymous: false
"""


def test_parse_validate_render_env():
    config = platform_config.parse_simple_yaml(SAMPLE)
    assert config["event"]["name"] == "Test Event"
    assert config["devices"]["switches"][1]["ip"] == "192.168.10.12"
    issues = platform_config.validate_config(config)
    assert not [item for item in issues if item["level"] == "bad"]
    env = platform_config.render_env(config, {"FEISHU_ROBOT_TOKEN": "keep-secret"})
    assert env["EVENT_NAME"] == "Test Event"
    assert env["CORE_SWITCH_PING"] == "192.168.10.254"
    assert env["DIST_SWITCH_PING"] == "stage-1:192.168.10.11,stage-2:192.168.10.12"
    assert env["FIREWALL_PING"] == "firewall:192.168.10.1"
    assert env["FIREWALL_SNMP_TARGETS"] == "firewall:192.168.10.1"
    assert env["ISP_PING"] == "telecom:223.5.5.5"
    assert env["BIGSCREEN_ISP_IPS"] == "telecom:203.0.113.10"
    assert env["FIREWALL_DISCOVERY_RANGE"] == "192.168.9.0/24"
    assert env["FEISHU_ROBOT_TOKEN"] == "keep-secret"
    assert env["GRAFANA_ANONYMOUS_ENABLED"] == "false"


def test_merge_env_preserves_unknown_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CUSTOM_KEEP=1\nEVENT_NAME=old\n", encoding="utf-8")
    rendered = platform_config.merge_env_file(env_path, {"EVENT_NAME": "new", "CORE_SWITCH_PING": "core:1.1.1.1"})
    assert "CUSTOM_KEEP=1" in rendered
    assert "EVENT_NAME=new" in rendered
    assert "CORE_SWITCH_PING=core:1.1.1.1" in rendered


def test_merge_env_repeated_applies_do_not_accumulate_comment_blocks(tmp_path):
    # Re-applying the same config (a common console workflow) must not keep
    # tacking on empty "# Generated ..." headers and blank lines every time.
    env_path = tmp_path / ".env"
    env_path.write_text("CORE_SWITCH_PING=old\nEXISTING=keep\n", encoding="utf-8")
    updates = {"CORE_SWITCH_PING": "core:1.1.1.1", "NEWKEY": "x"}
    for _ in range(3):
        env_path.write_text(platform_config.merge_env_file(env_path, updates), encoding="utf-8")
    rendered = env_path.read_text(encoding="utf-8")
    assert rendered.count("# Generated from event-config.yml") == 1
    assert "EXISTING=keep" in rendered
    assert "NEWKEY=x" in rendered
    assert "\n\n\n" not in rendered


def test_blank_yaml_values_are_empty_strings_and_gateway_can_be_empty():
    config = platform_config.parse_simple_yaml("""
event:
  name:
networks:
  player_gateways:
devices:
  core:
    ip: 192.168.10.254
  switches:
    - name: stage-1
      ip: 192.168.10.11
isp:
  links:
    - name: telecom
      gateway:
""")
    assert config["event"]["name"] == ""
    assert config["isp"]["links"][0]["gateway"] == ""
    env = platform_config.render_env(config)
    # A blank player_gateways parses cleanly; the gateway then defaults to the
    # core switch IP (the Cisco core is the player L3 gateway).
    assert env["PLAYER_GATEWAYS"] == "192.168.10.254"


def test_platform_fields_render_frontend_config():
    config = platform_config.parse_simple_yaml("""
networks:
  player_subnets: 192.168.40.0/24
  switch_management_ranges: 192.168.10.1-100,192.168.10.254
  firewall_management_ranges: 192.168.10.1-2
snmp:
  community: esport-snmp
devices:
  core:
    ip: 192.168.10.254
  firewall:
    ip: 192.168.10.1
    snmp: 192.168.10.1,192.168.10.2
    unit_snmp: 192.168.10.11,192.168.10.12
  stage_switches:
    - ip: 192.168.10.11
    - ip: 192.168.10.12
  access_switches:
    - ip: 192.168.10.21
isp:
  wan_if_filter: telecom,WAN
  max_bandwidth_mbps:
  links:
unifi:
  password: secret
  verify_ssl: true
""")
    env = platform_config.render_env(config)
    assert env["SNMP_COMMUNITY"] == "esport-snmp"
    assert env["DIST_SWITCH_PING"] == "192.168.10.11,192.168.10.12,192.168.10.21"
    assert env["TOURNAMENT_SWITCHES"] == "192.168.10.11,192.168.10.12"
    assert env["FIREWALL_SNMP_TARGETS"] == "192.168.10.1,192.168.10.2"
    assert env["FIREWALL_UNIT_SNMP_TARGETS"] == "192.168.10.11,192.168.10.12"
    assert env["LIBRENMS_DISCOVERY_TARGETS"] == "192.168.10.1-100,192.168.10.254"
    assert env["FIREWALL_DISCOVERY_RANGE"] == "192.168.10.1-2"
    assert env["FIREWALL_WAN_IF_FILTER"] == "telecom,WAN"
    assert env["BIGSCREEN_ISP_MAX_BANDWIDTH"] == "1000"
    assert env["ISP_SATURATION_PERCENT"] == "90"
    assert env["ISP_DOWN_FOR_SECONDS"] == "10"
    assert env["UNIFI_CONTROLLER_PASS"] == "secret"
    assert env["UNIFI_CONTROLLER_VERIFY_SSL"] == "true"


def test_per_link_bandwidth_is_rendered_in_form_order():
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 192.168.10.254
isp:
  max_bandwidth_mbps:
  links:
    - name:
      ip: 222.72.19.238
      ping: 222.72.19.237
      bandwidth_mbps: 200
    - name:
      ip: 58.246.24.67
      ping: 58.246.24.65
      bandwidth_mbps: 500
""")
    env = platform_config.render_env(config)
    assert env["BIGSCREEN_ISP_MAX_BANDWIDTH"] == "*:1000,__link_1:200,__link_2:500"


def test_missing_per_link_bandwidth_keeps_its_position_and_uses_global_default():
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 192.168.10.254
isp:
  max_bandwidth_mbps: 800
  links:
    - name: telecom
      ip: 203.0.113.10
    - name:
      ip: 203.0.113.11
      bandwidth_mbps: 500
""")
    env = platform_config.render_env(config)
    assert env["BIGSCREEN_ISP_MAX_BANDWIDTH"] == "*:800,telecom:800,__link_2:500"


def test_empty_stage_switches_do_not_fall_back_to_legacy_switches():
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 192.168.10.254
  switches:
    - ip: 192.168.10.11
    - ip: 192.168.10.12
  stage_switches: []
  access_switches: []
""")
    env = platform_config.render_env(config)
    assert env["TOURNAMENT_SWITCHES"] == ""
    assert env["DIST_SWITCH_PING"] == ""


def test_empty_switch_list_drives_discovery_from_range():
    # Operator fills only the core IP + switch management range: no static switch
    # ping targets are emitted (offline IPs must not be pinged); instead the
    # discovery loop gets the range, the core doubles as the gateway, and the
    # missing-stage-switch warning is suppressed.
    config = platform_config.parse_simple_yaml("""
networks:
  switch_management_ranges: 192.168.10.11-30,192.168.10.254
devices:
  core:
    ip: 192.168.10.254
  stage_switches: []
  access_switches: []
""")
    env = platform_config.render_env(config)
    assert env["DIST_SWITCH_PING"] == ""
    assert env["TOURNAMENT_SWITCHES"] == ""
    # Core IP is dropped from the discovery range (it has its own ping/SNMP target).
    assert env["SWITCH_DISCOVERY_RANGE"] == "192.168.10.11-30"
    assert env["PLAYER_GATEWAYS"] == "192.168.10.254"
    assert env["LIBRENMS_CORE_IP"] == "192.168.10.254"
    issues = platform_config.validate_config(config)
    assert not [i for i in issues if i["level"] == "bad"]
    assert not [i for i in issues if i["path"] == "devices.stage_switches"]


def test_explicit_switches_and_discovery_range_coexist():
    config = platform_config.parse_simple_yaml("""
networks:
  switch_management_ranges: 192.168.10.11-30
devices:
  core:
    ip: 192.168.10.254
  stage_switches:
    - name: 舞台A
      ip: 192.168.10.11
  access_switches: []
""")
    env = platform_config.render_env(config)
    assert env["DIST_SWITCH_PING"] == "舞台A:192.168.10.11"
    assert env["TOURNAMENT_SWITCHES"] == "舞台A:192.168.10.11"
    assert env["SWITCH_DISCOVERY_RANGE"] == "192.168.10.11-30"


def test_switch_range_cidr_drives_discovery_and_librenms():
    # A CIDR block is both SNMP-probed by the discovery loop (ICMP gates each
    # address, so a sparse /24 stays cheap) and handed to LibreNMS discovery.
    config = platform_config.parse_simple_yaml("""
networks:
  switch_management_ranges: 192.168.10.0/24
devices:
  core:
    ip: 192.168.10.254
  stage_switches: []
  access_switches: []
""")
    env = platform_config.render_env(config)
    assert env["DIST_SWITCH_PING"] == ""
    assert env["SWITCH_DISCOVERY_RANGE"] == "192.168.10.0/24"
    assert env["LIBRENMS_DISCOVERY_TARGETS"] == "192.168.10.0/24"


def test_explicit_player_gateway_overrides_core_default():
    config = platform_config.parse_simple_yaml("""
networks:
  player_gateways: 192.168.40.1
devices:
  core:
    ip: 192.168.10.254
""")
    env = platform_config.render_env(config)
    assert env["PLAYER_GATEWAYS"] == "192.168.40.1"


def test_isp_public_ip_is_required_when_link_is_configured():
    # 关闭自动发现且该行也没有探测目标时仍强制公网 IP（旧行为）
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 192.168.10.254
isp:
  auto_discovery: false
  links:
    - name: telecom
      bandwidth_mbps: 500
""")
    issues = platform_config.validate_config(config)
    assert [item for item in issues if item["level"] == "bad" and item["path"] == "isp.links[0].ip"]


def test_isp_link_with_ping_target_not_blocked_when_discovery_off():
    # 关闭自动发现但行里有 ping 探测目标：公网 IP 只影响拓扑展示，降为 warn，
    # 预检不再把这种可用配置当成部署阻塞项
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 192.168.10.254
isp:
  auto_discovery: false
  links:
    - name: telecom
      ping: 219.140.134.161
""")
    issues = platform_config.validate_config(config)
    assert not [item for item in issues if item["level"] == "bad" and item["path"] == "isp.links[0].ip"]
    assert [item for item in issues if item["level"] == "warn" and item["path"] == "isp.links[0].ip"]


def test_isp_bandwidth_only_link_is_allowed_with_auto_discovery():
    # 自动发现（默认开）时，只填名称+带宽用于绑定 WAN 口的行不再阻塞保存
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 192.168.10.254
isp:
  links:
    - name: telecom
      bandwidth_mbps: 500
""")
    issues = platform_config.validate_config(config)
    assert not [item for item in issues if item["level"] == "bad" and item["path"] == "isp.links[0].ip"]
    assert [item for item in issues if item["level"] == "warn" and item["path"] == "isp.links[0].ip"]
    env = platform_config.render_env(config)
    assert env["ISP_GATEWAY_AUTO_DISCOVER"] == "true"
    assert env["BIGSCREEN_ISP_MAX_BANDWIDTH"] == "*:1000,telecom:500"


def test_runtime_allows_duplicate_sysnames_for_identical_aps():
    script = (ROOT / "librenms-auto-config.sh").read_text(encoding="utf-8")
    assert "config:set allow_duplicate_sysName true" in script


def test_yaml_dump_and_parse_round_trip_escaped_strings():
    original = {
        "event": {
            "name": 'Arena "A" \\ Main',
            "subtitle": "line one\nline two # final",
        },
        "devices": {"core": {"ip": "192.168.10.254"}},
    }
    rendered = platform_config.dump_simple_yaml(original)
    assert platform_config.parse_simple_yaml(rendered) == original


def test_env_round_trip_preserves_spaces_hash_quotes_and_backslashes(tmp_path):
    env_path = tmp_path / ".env"
    values = {
        "SNMP_COMMUNITY": 'event #1 "main" \\ site',
        "PLAIN": "ok",
    }
    env_path.write_text(platform_config.merge_env_file(env_path, values), encoding="utf-8")
    assert platform_config.read_env(env_path) == values


def test_validation_rejects_invalid_network_and_numeric_values():
    config = platform_config.parse_simple_yaml("""
devices:
  core:
    ip: 999.999.999.999
  stage_switches:
    - name: bad
      ip: bad-ip
networks:
  player_vlan: 9999
  wireless_vlan: 0
  player_subnets: not-a-cidr
  player_gateways: also-bad
  switch_management_ranges: 192.168.0.0/16
isp:
  saturation_percent: -1
  down_for_seconds: 0
  links:
    - ip: 203.0.113.999
      ping: nope
      bandwidth_mbps: -100
""")
    bad_paths = {item["path"] for item in platform_config.validate_config(config) if item["level"] == "bad"}
    assert "devices.core.ip" in bad_paths
    assert "devices.stage_switches[0].ip" in bad_paths
    assert "networks.player_vlan[0]" in bad_paths
    assert "networks.wireless_vlan[0]" in bad_paths
    assert "networks.player_subnets[0]" in bad_paths
    assert "networks.player_gateways[0]" in bad_paths
    assert "networks.switch_management_ranges[0]" in bad_paths
    assert "isp.saturation_percent" in bad_paths
    assert "isp.down_for_seconds" in bad_paths
    assert "isp.links[0].ip" in bad_paths
    assert "isp.links[0].ping" in bad_paths
    assert "isp.links[0].bandwidth_mbps" in bad_paths


def test_validation_reports_wrong_section_types_without_crashing():
    issues = platform_config.validate_config({
        "devices": ["not", "an", "object"],
        "networks": "bad",
        "isp": {"links": "bad"},
    })
    paths = {item["path"] for item in issues if item["level"] == "bad"}
    assert {"devices", "networks", "isp.links"}.issubset(paths)
    # Rendering invalid sections is safe after validation, so the API can return
    # structured issues instead of an AttributeError/HTTP 500.
    assert isinstance(platform_config.render_env({"devices": [], "networks": "bad"}), dict)


def test_unifi_profile_can_be_disabled_without_dropping_other_profiles():
    existing = {"COMPOSE_PROFILES": "metrics,unifi"}
    disabled = platform_config.render_env({"devices": {"core": {"ip": "192.168.10.254"}}, "unifi": {"enabled": False}}, existing)
    enabled = platform_config.render_env({"devices": {"core": {"ip": "192.168.10.254"}}, "unifi": {"enabled": True}}, existing)
    assert disabled["COMPOSE_PROFILES"] == "metrics"
    assert enabled["COMPOSE_PROFILES"] == "metrics,unifi"

if __name__ == "__main__":
    test_parse_validate_render_env()
    test_blank_yaml_values_are_empty_strings_and_gateway_can_be_empty()
    test_platform_fields_render_frontend_config()
    test_per_link_bandwidth_is_rendered_in_form_order()
    test_missing_per_link_bandwidth_keeps_its_position_and_uses_global_default()
    test_empty_stage_switches_do_not_fall_back_to_legacy_switches()
    test_isp_public_ip_is_required_when_link_is_configured()
    test_isp_link_with_ping_target_not_blocked_when_discovery_off()
    test_isp_bandwidth_only_link_is_allowed_with_auto_discovery()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        test_merge_env_preserves_unknown_keys(Path(tmp))
    print("platform config tests passed")
