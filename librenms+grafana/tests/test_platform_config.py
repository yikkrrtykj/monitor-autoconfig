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
    assert env["BIGSCREEN_EVENT_MODE"] == "monitor"
    assert env["CORE_SWITCH_PING"] == "core:192.168.10.254"
    assert env["DIST_SWITCH_PING"] == "stage-1:192.168.10.11,stage-2:192.168.10.12"
    assert env["FIREWALL_PING"] == "firewall:192.168.10.1"
    assert env["FIREWALL_SNMP_TARGETS"] == "firewall:192.168.10.1"
    assert env["ISP_PING"] == "telecom:223.5.5.5"
    assert env["BIGSCREEN_ISP_IPS"] == "telecom:203.0.113.10"
    assert env["FIREWALL_DISCOVERY_RANGE"] == "192.168.9.0/24"
    assert env["UNIFI_AP_DOWN_FOR_SECONDS"] == "180"
    assert env["FEISHU_ROBOT_TOKEN"] == "keep-secret"
    assert env["GRAFANA_ANONYMOUS_ENABLED"] == "false"


def test_merge_env_preserves_unknown_keys(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("CUSTOM_KEEP=1\nEVENT_NAME=old\n", encoding="utf-8")
    rendered = platform_config.merge_env_file(env_path, {"EVENT_NAME": "new", "CORE_SWITCH_PING": "core:1.1.1.1"})
    assert "CUSTOM_KEEP=1" in rendered
    assert "EVENT_NAME=new" in rendered
    assert "CORE_SWITCH_PING=core:1.1.1.1" in rendered


def test_blank_yaml_values_are_empty_strings_and_gateway_falls_back():
    config = platform_config.parse_simple_yaml("""
event:
  name:
  public_base_url:
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
    assert config["event"]["public_base_url"] == ""
    assert config["isp"]["links"][0]["gateway"] == ""
    env = platform_config.render_env(config)
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


if __name__ == "__main__":
    test_parse_validate_render_env()
    test_blank_yaml_values_are_empty_strings_and_gateway_falls_back()
    test_platform_fields_render_frontend_config()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        test_merge_env_preserves_unknown_keys(Path(tmp))
    print("platform config tests passed")
