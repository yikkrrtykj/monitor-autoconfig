;(function () {
  'use strict';

  function cloneControlConfig(configValue) {
    return JSON.parse(JSON.stringify(configValue || {}));
  }

  function asConfigArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function configScalar(value) {
    if (value == null) return "";
    if (Array.isArray(value)) return value.join("\n");
    if (typeof value === "object") return "";
    return String(value);
  }

  function csvText(value) {
    if (Array.isArray(value)) return value.join("\n");
    return configScalar(value);
  }

  function splitConfigList(value) {
    return String(value || "")
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function controlConfigDefaults(configValue) {
    const value = cloneControlConfig(configValue);
    value.event = { name: "", default_layout: "tournament-64-2layer", team_orders: {}, ...(value.event || {}) };
    if (!value.event.team_orders || typeof value.event.team_orders !== "object" || Array.isArray(value.event.team_orders)) {
      value.event.team_orders = {};
    }
    // Public access is not a control-panel concern. Older imported configs may
    // still contain these keys; drop them when the form is saved so the basic
    // section stays limited to the event name and default tournament layout.
    delete value.event.security_mode;
    delete value.event.public_base_url;
    if (String(value.event.name || "").trim() === "武汉斗鱼嘉年华") {
      value.event.name = "";
    }
    value.networks = { player_vlan: 40, wireless_vlan: 41, firewall_management_ranges: "", ...(value.networks || {}) };
    value.snmp = { community: "global", ...(value.snmp || {}) };
    value.devices = { switches: [], servers: [], ...(value.devices || {}) };
    value.devices.core = { ...(value.devices.core || {}) };
    value.devices.firewall = { ...(value.devices.firewall || {}) };
    if (String(value.devices.core.name || "").trim().toLowerCase() === "core") {
      value.devices.core.name = "";
    }
    if (!configScalar(value.devices.firewall.ip) && configScalar(value.devices.firewall.snmp)) {
      value.devices.firewall.ip = value.devices.firewall.snmp;
    }
    if (String(value.devices.firewall.ip || "").trim() === String(value.devices.firewall.snmp || "").trim()) {
      value.devices.firewall.snmp = "";
    }
    if (String(value.networks.player_gateways || "") === String(value.devices.core.ip || "")) {
      value.networks.player_gateways = "";
    }
    const hasStageSwitches = Object.prototype.hasOwnProperty.call(value.devices, "stage_switches");
    const legacySwitches = asConfigArray(value.devices.switches);
    value.devices.stage_switches = asConfigArray(value.devices.stage_switches);
    value.devices.access_switches = asConfigArray(value.devices.access_switches);
    if (!hasStageSwitches && !value.devices.stage_switches.length && legacySwitches.length) {
      value.devices.stage_switches = legacySwitches;
    }
    value.devices.servers = asConfigArray(value.devices.servers).map((item) => ({
      name: item.name || "",
      ip: item.ip || item.target || ""
    }));
    value.devices.stage_switches = value.devices.stage_switches.map((item) => ({ ...item, name: item.name || "", ip: item.ip || item.target || "" }));
    value.devices.access_switches = value.devices.access_switches.map((item) => ({ ...item, name: item.name || "", ip: item.ip || item.target || "" }));
    if (
      value.devices.servers.length === 1
      && ["grafana", "game server"].includes(String(value.devices.servers[0].name || "").toLowerCase())
      && String(value.devices.servers[0].ip || "") === "192.168.41.253"
    ) {
      value.devices.servers = [];
    } else if (
      value.devices.servers.length === 1
      && String(value.devices.servers[0].name || "").toLowerCase() === "game server"
      && !String(value.devices.servers[0].ip || "").trim()
    ) {
      value.devices.servers = [];
    }
    value.isp = {
      auto_discovery: true,
      wan_if_filter: "telecom,telcom,unicom,isp,WAN",
      max_bandwidth_mbps: 1000,
      links: [],
      ...(value.isp || {})
    };
    value.isp.links = asConfigArray(value.isp.links);
    if (!value.isp.links.length && Number(value.isp.max_bandwidth_mbps) === 1000) {
      value.isp.max_bandwidth_mbps = "";
    }
    value.unifi = { enabled: false, password: "", sites: "all", verify_ssl: false, ...(value.unifi || {}) };
    value.alerts = {
      syslog_alert_types: "native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback",
      gateway_macs: "",
      gateway_uplink_ports: "",
      mac_flap_window_seconds: 60,
      mac_flap_threshold: 3,
      cpu_alert_percent: 70,
      memory_alert_percent: 80,
      ...(value.alerts || {})
    };
    if (value.alerts.syslog_alert_types === "native_vlan_mismatch,errdisable,bpduguard,loopback") {
      value.alerts.syslog_alert_types = "native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback";
    }
    value.security = { ...(value.security || {}), grafana_anonymous: (value.security || {}).grafana_anonymous !== false };
    return value;
  }

  function configPathGet(object, path) {
    return path.split(".").reduce((current, key) => current && current[key], object);
  }

  function configPathSet(object, path, value) {
    const parts = path.split(".");
    let current = object;
    parts.slice(0, -1).forEach((key) => {
      if (!current[key] || typeof current[key] !== "object") current[key] = {};
      current = current[key];
    });
    current[parts[parts.length - 1]] = value;
  }

  function expandIpRangeText(value) {
    const expanded = [];
    splitConfigList(value).forEach((raw) => {
      const item = String(raw || "").trim();
      if (!item) return;
      const full = item.match(/^(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3}(?:\.\d{1,3}){3})$/);
      const short = item.match(/^(\d{1,3}\.\d{1,3}\.\d{1,3}\.)(\d{1,3})-(\d{1,3})$/);
      if (full) {
        const start = full[1].split(".").map(Number);
        const end = full[2].split(".").map(Number);
        if (start.slice(0, 3).join(".") === end.slice(0, 3).join(".") && start[3] <= end[3]) {
          for (let octet = start[3]; octet <= end[3]; octet += 1) expanded.push(`${start[0]}.${start[1]}.${start[2]}.${octet}`);
          return;
        }
      }
      if (short) {
        const start = Number(short[2]);
        const end = Number(short[3]);
        if (start <= end) {
          for (let octet = start; octet <= end; octet += 1) expanded.push(`${short[1]}${octet}`);
          return;
        }
      }
      expanded.push(item);
    });
    return expanded;
  }

  const ns = {
    cloneControlConfig,
    asConfigArray,
    configScalar,
    csvText,
    splitConfigList,
    controlConfigDefaults,
    configPathGet,
    configPathSet,
    expandIpRangeText
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSConfigModel = ns;
  }
}());
