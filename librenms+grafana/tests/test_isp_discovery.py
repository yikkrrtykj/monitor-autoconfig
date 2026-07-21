import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "discover-isp-targets.py"
spec = importlib.util.spec_from_file_location("discover_isp_targets", MODULE_PATH)
disco = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(disco)


def _hillstone_walks():
    """两条 WAN(电信/联通,别名进 SNMP)+ 一个内网口,双默认路由。"""
    return {
        disco.OID_IF_ALIAS: {
            f"{disco.OID_IF_ALIAS}.1": "电信",
            f"{disco.OID_IF_ALIAS}.2": "联通",
            f"{disco.OID_IF_ALIAS}.3": "lan",
        },
        disco.OID_IF_NAME: {
            f"{disco.OID_IF_NAME}.1": "ethernet0/0",
            f"{disco.OID_IF_NAME}.2": "ethernet0/1",
            f"{disco.OID_IF_NAME}.3": "ethernet0/2",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.64.1.2": "1",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.65.1.2": "2",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.192.168.9.1": "3",
        },
        disco.OID_IP_AD_ENT_NETMASK: {
            f"{disco.OID_IP_AD_ENT_NETMASK}.100.64.1.2": "255.255.255.0",
            f"{disco.OID_IP_AD_ENT_NETMASK}.100.65.1.2": "255.255.255.0",
            f"{disco.OID_IP_AD_ENT_NETMASK}.192.168.9.1": "255.255.255.0",
        },
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.64.1.1": "100.64.1.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.65.1.1": "100.65.1.1",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.64.1.1": "1",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.65.1.1": "2",
        },
    }


def test_parse_walk_strips_types_and_quotes():
    parsed = disco.parse_walk(
        '.1.3.6.1.2.1.31.1.1.1.18.1 = STRING: "电信"\n'
        ".1.3.6.1.2.1.4.20.1.2.100.64.1.2 = INTEGER: 1\n"
        ".1.3.6.1.2.1.4.24.4.1.4.0.0.0.0.0.0.0.0.0.100.64.1.1 = IpAddress: 100.64.1.1\n"
        ".1.3.6.1.2.1.31.1.1.1.18.9 = No Such Instance currently exists\n"
    )
    assert parsed[".1.3.6.1.2.1.31.1.1.1.18.1"] == "电信"
    assert parsed[".1.3.6.1.2.1.4.20.1.2.100.64.1.2"] == "1"
    assert ".1.3.6.1.2.1.31.1.1.1.18.9" not in parsed


def test_discovers_multi_wan_gateways_named_by_interface():
    results = disco.discover_from_walks(_hillstone_walks(), disco.wan_keywords("telecom,unicom,电信,联通"))
    assert [(item["name"], item["gateway"], item["wan_ip"]) for item in results] == [
        ("电信", "100.64.1.1", "100.64.1.2"),
        ("联通", "100.65.1.1", "100.65.1.2"),
    ]


def test_wan_keyword_digit_boundary_matches_like_bridge():
    keywords = disco.wan_keywords("eth0,eth1")
    assert disco.is_wan_label("eth1", keywords)
    assert not disco.is_wan_label("eth10", keywords)


def test_subnet_fallback_when_route_has_no_ifindex():
    walks = _hillstone_walks()
    walks.pop(disco.OID_CIDR_DEFAULT_IFINDEX)
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert {item["gateway"] for item in results} == {"100.64.1.1", "100.65.1.1"}


def test_rfc1213_fallback_single_default_route():
    walks = _hillstone_walks()
    walks.pop(disco.OID_CIDR_DEFAULT_NEXTHOP)
    walks.pop(disco.OID_CIDR_DEFAULT_IFINDEX)
    walks[disco.OID_ROUTE_DEFAULT_NEXTHOP] = {disco.OID_ROUTE_DEFAULT_NEXTHOP: "100.64.1.1"}
    walks[disco.OID_ROUTE_DEFAULT_IFINDEX] = {disco.OID_ROUTE_DEFAULT_IFINDEX: "1"}
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert [(item["name"], item["gateway"]) for item in results] == [("电信", "100.64.1.1")]


def test_lan_default_route_and_duplicates_are_dropped():
    walks = _hillstone_walks()
    # 一条经内网口的默认路由(如管理旁路)不算 ISP;重复下一跳只留一条
    walks[disco.OID_CIDR_DEFAULT_NEXTHOP][f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.192.168.9.254"] = "192.168.9.254"
    walks[disco.OID_CIDR_DEFAULT_IFINDEX][f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.192.168.9.254"] = "3"
    walks[disco.OID_CIDR_DEFAULT_NEXTHOP][f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.1.100.64.1.1"] = "100.64.1.1"
    walks[disco.OID_CIDR_DEFAULT_IFINDEX][f"{disco.OID_CIDR_DEFAULT_IFINDEX}.1.100.64.1.1"] = "1"
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert {item["gateway"] for item in results} == {"100.64.1.1", "100.65.1.1"}


def test_manual_isp_ping_entries_take_precedence():
    results = disco.discover_from_walks(_hillstone_walks(), disco.wan_keywords("电信,联通"))
    payload = disco.build_file_sd(results, exclude={"100.64.1.1"})
    assert [entry["targets"][0] for entry in payload] == ["100.65.1.1"]
    assert payload[0]["labels"]["display_name"] == "联通"
    assert payload[0]["labels"]["wan_ip"] == "100.65.1.2"


def test_duplicate_carrier_lines_numbered_by_ifindex():
    """双电信双联通:同名口按 ifIndex 升序编号,和带宽告警的去重规则一致。"""
    walks = {
        disco.OID_IF_ALIAS: {
            f"{disco.OID_IF_ALIAS}.4": "电信",
            f"{disco.OID_IF_ALIAS}.2": "电信",
            f"{disco.OID_IF_ALIAS}.5": "联通",
            f"{disco.OID_IF_ALIAS}.6": "联通",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.64.1.2": "2",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.64.2.2": "4",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.65.1.2": "5",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.65.2.2": "6",
        },
        disco.OID_IP_AD_ENT_NETMASK: {},
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.64.1.1": "100.64.1.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.64.2.1": "100.64.2.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.65.1.1": "100.65.1.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.65.2.1": "100.65.2.1",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.64.1.1": "2",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.64.2.1": "4",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.65.1.1": "5",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.65.2.1": "6",
        },
    }
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert [(item["name"], item["gateway"]) for item in results] == [
        ("电信-1", "100.64.1.1"),
        ("电信-2", "100.64.2.1"),
        ("联通-1", "100.65.1.1"),
        ("联通-2", "100.65.2.1"),
    ]


def test_foreign_carrier_names_match_by_keyword():
    """国外运营商:关键词/口名没有任何语言假设,配进 WAN 过滤即可。"""
    walks = {
        disco.OID_IF_ALIAS: {
            f"{disco.OID_IF_ALIAS}.1": "Vodafone-Line",
            f"{disco.OID_IF_ALIAS}.2": "Singtel",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.203.0.113.2": "1",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.198.51.100.2": "2",
        },
        disco.OID_IP_AD_ENT_NETMASK: {},
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.203.0.113.1": "203.0.113.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.198.51.100.1": "198.51.100.1",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.203.0.113.1": "1",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.198.51.100.1": "2",
        },
    }
    results = disco.discover_from_walks(walks, disco.wan_keywords("vodafone,singtel"))
    assert {item["name"] for item in results} == {"Vodafone-Line", "Singtel"}


def test_generic_interface_names_use_public_default_routes_and_console_names():
    """A firewall may expose only ethernet0/x while the console owns the labels."""
    walks = {
        disco.OID_IF_ALIAS: {},
        disco.OID_IF_NAME: {
            f"{disco.OID_IF_NAME}.1": "ethernet0/0",
            f"{disco.OID_IF_NAME}.3": "ethernet0/2",
            f"{disco.OID_IF_NAME}.5": "ethernet0/4",
            f"{disco.OID_IF_NAME}.7": "ethernet0/6",
            f"{disco.OID_IF_NAME}.9": "ethernet0/8",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.101.95.176.198": "1",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.116.238.242.155": "3",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.116.128.201.226": "5",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.61.169.238.58": "7",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.192.168.9.1": "9",
        },
        disco.OID_IP_AD_ENT_NETMASK: {
            f"{disco.OID_IP_AD_ENT_NETMASK}.101.95.176.198": "255.255.255.252",
            f"{disco.OID_IP_AD_ENT_NETMASK}.116.238.242.155": "255.255.255.248",
            f"{disco.OID_IP_AD_ENT_NETMASK}.116.128.201.226": "255.255.255.240",
            f"{disco.OID_IP_AD_ENT_NETMASK}.61.169.238.58": "255.255.255.248",
            f"{disco.OID_IP_AD_ENT_NETMASK}.192.168.9.1": "255.255.255.0",
        },
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.101.95.176.197": "101.95.176.197",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.116.238.242.153": "116.238.242.153",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.116.128.201.225": "116.128.201.225",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.61.169.238.57": "61.169.238.57",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.192.168.9.254": "192.168.9.254",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.101.95.176.197": "1",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.116.238.242.153": "3",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.116.128.201.225": "5",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.61.169.238.57": "7",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.192.168.9.254": "9",
        },
    }
    names = ["telcom-100M-长期", "telcom-1000M", "unicom-1000M", "telcom-100M"]
    results = disco.discover_from_walks(walks, disco.wan_keywords("telecom,unicom,WAN"), names)
    assert [(item["name"], item["wan_ip"], item["gateway"]) for item in results] == [
        ("telcom-1000M", "116.238.242.155", "116.238.242.153"),
        ("telcom-100M", "61.169.238.58", "61.169.238.57"),
        ("telcom-100M-长期", "101.95.176.198", "101.95.176.197"),
        ("unicom-1000M", "116.128.201.226", "116.128.201.225"),
    ]


def test_target_ips_parses_named_lists():
    assert disco.target_ips("FW:192.168.9.1, 192.168.9.2\ntelecom:1.2.3.4") == [
        "192.168.9.1", "192.168.9.2", "1.2.3.4",
    ]


if __name__ == "__main__":
    test_parse_walk_strips_types_and_quotes()
    test_discovers_multi_wan_gateways_named_by_interface()
    test_wan_keyword_digit_boundary_matches_like_bridge()
    test_subnet_fallback_when_route_has_no_ifindex()
    test_rfc1213_fallback_single_default_route()
    test_lan_default_route_and_duplicates_are_dropped()
    test_manual_isp_ping_entries_take_precedence()
    test_duplicate_carrier_lines_numbered_by_ifindex()
    test_foreign_carrier_names_match_by_keyword()
    test_target_ips_parses_named_lists()
    print("ISP discovery tests passed")
