;(function (root, factory) {
  const ns = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = ns;
  else root.BSNetworkRead = ns;
}(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const DOMAINS = ["devices", "topology", "isp"];

  function deviceStatus(status) {
    return status === "up" ? true : (status === "down" ? false : null);
  }

  function networkDomainState(payload, name) {
    if (!payload || payload.ok !== true) return "unavailable";
    const field = { devices: "devices", topology: "edges", isp: "isps" }[name];
    if (field && !Array.isArray(payload[field])) return "unavailable";
    if (payload.stale === true) return "stale";
    return payload.degraded === true ? "degraded" : "normal";
  }

  function networkErrorInfo(error) {
    if (!error) return null;
    const payload = error.payload || error;
    return {
      status: error.status || null,
      code: payload.code || null,
      error: payload.error || error.message || null
    };
  }

  function normalizeNetworkOverview(payload) {
    return Object.fromEntries(DOMAINS.map((name) => [name, payload && payload[name] || null]));
  }

  function mergeNetworkDevices(devices, enrichment) {
    const byIp = new Map((enrichment || []).map((item) => [String(item.targetIp || item.instance || ""), item]));
    return (devices || []).map((device) => {
      const ip = String(device.ip || device.hostname || "");
      const metric = byIp.get(ip) || {};
      return {
        ...metric,
        instance: metric.instance || ip,
        targetIp: ip,
        displayName: metric.displayName || device.name || ip,
        job: metric.job || "",
        success: deviceStatus(device.status),
        status: device.status,
        latency: Number.isFinite(metric.latency) ? metric.latency : null
      };
    });
  }

  function overlayTopologyDeviceStatus(targets, devices) {
    const byIp = new Map((devices || []).filter((device) => device && device.ip)
      .map((device) => [String(device.ip), device]));
    return (targets || []).map((target) => {
      if (target.job === "infra-isp-ping") return target;
      const device = byIp.get(String(target.targetIp || target.instance || ""));
      return device ? { ...target, success: deviceStatus(device.status), status: device.status } : target;
    });
  }

  function topologyNetworkTargets(domains, enrichment, seenUp) {
    const deviceDomain = domains.devices;
    const ispDomain = domains.isp;
    const deviceTargets = deviceDomain.source === "network-api"
      ? overlayTopologyDeviceStatus(enrichment, deviceDomain.data.devices)
      : (deviceDomain.data || []);
    const ispTargets = ispDomain.source === "network-api"
      ? mergeIspInventory(ispDomain.data.isps, enrichment)
      : mergeLegacyIspInventory(ispDomain.data, enrichment);
    const combined = deviceTargets.filter((item) => item.job !== "infra-isp-ping").concat(ispTargets);
    return seenUp && seenUp.size
      ? combined.filter((target) => target.job === "infra-fw-unit-snmp" || target.job === "infra-isp-ping" ||
        seenUp.has(target.instance))
      : combined;
  }

  function networkIssueNotice(domains) {
    const labels = { devices: "设备", topology: "拓扑", isp: "ISP" };
    const states = { degraded: "数据降级", stale: "快照陈旧", unavailable: "不可用" };
    const issues = Object.entries(domains || {}).filter(([, domain]) => domain && states[domain.state]);
    if (!issues.length) return null;
    const severity = ["unavailable", "stale", "degraded"].find((state) => issues.some(([, domain]) => domain.state === state));
    return {
      state: severity,
      text: issues.map(([name, domain]) => `${labels[name] || name}${states[domain.state]}`).join(" · ")
    };
  }

  function renderNetworkIssue(element, domains) {
    const notice = networkIssueNotice(domains);
    element.dataset.state = notice ? notice.state : "";
    element.textContent = notice ? notice.text : "";
    element.hidden = !notice;
  }

  function mergeIspInventory(isps, enrichment) {
    const byIp = new Map((enrichment || []).filter((item) => item.job === "infra-isp-ping")
      .map((item) => [String(item.targetIp || item.instance || ""), item]));
    return (isps || []).map((isp) => {
      const ip = String(isp.target || "");
      const metric = byIp.get(ip) || {};
      return {
        ...metric,
        job: "infra-isp-ping",
        instance: metric.instance || ip || isp.name,
        targetIp: ip,
        displayName: isp.name,
        name: isp.name,
        wanIp: isp.wanIp || "",
        metricTarget: isp.metricTarget || "",
        metricIfindex: isp.metricIfindex || "",
        success: deviceStatus(isp.status),
        status: isp.status,
        latency: Number.isFinite(metric.latency) ? metric.latency : null
      };
    });
  }

  function mergeLegacyIspInventory(inventory, enrichment) {
    const ispMetrics = (enrichment || []).filter((item) => item.job === "infra-isp-ping");
    const identity = (value) => String(value || "").trim().toLocaleLowerCase();
    const uniqueMatch = (value, field) => {
      const key = identity(value);
      if (!key) return null;
      const matches = ispMetrics.filter((metric) => !used.has(metric) && identity(field(metric)) === key);
      return matches.length === 1 ? matches[0] : null;
    };
    const used = new Set();
    const merged = mergeIspInventory((inventory || []).map((item) => {
      const configuredTarget = item.gateway || item.target || item.targetIp || item.ip;
      const metric = uniqueMatch(configuredTarget, (entry) => entry.targetIp || entry.instance) ||
        uniqueMatch(item.name, (entry) => entry.displayName);
      if (metric) used.add(metric);
      const target = String(metric ? (metric.targetIp || metric.instance || "") : (configuredTarget || ""));
      return {
        name: item.name, target, wanIp: item.wanIp,
        metricTarget: item.metricTarget, metricIfindex: item.metricIfindex,
        status: metric ? (metric.success === true ? "up" : (metric.success === false ? "down" : "unknown")) : "unknown"
      };
    }), enrichment);
    return merged.concat(ispMetrics.filter((item) => !used.has(item)));
  }

  async function controlNetworkTargets(domains, fetchEnrichment) {
    const devices = domains.devices;
    const isp = domains.isp;
    const enrichment = isp.source === "legacy"
      ? (devices.source === "legacy" ? devices.data || [] : await fetchEnrichment().catch(() => []))
      : [];
    const ispTargets = isp.source === "network-api"
      ? mergeIspInventory(isp.data.isps)
      : mergeLegacyIspInventory(isp.data, enrichment);
    return devices.source === "network-api"
      ? mergeNetworkDevices(devices.data.devices).concat(ispTargets)
      : (devices.data || []).filter((item) => item.job !== "infra-isp-ping").concat(ispTargets);
  }

  async function resolveNetworkDomain(payload, fallback, name) {
    if (networkDomainState(payload, name) !== "unavailable") {
      return { data: payload, state: networkDomainState(payload, name), source: "network-api" };
    }
    try {
      const data = await fallback();
      if (data === null || data === undefined) throw new Error("legacy data unavailable");
      return { data, state: "legacy-fallback", source: "legacy" };
    } catch (error) {
      return { data: null, state: "unavailable", source: "none", error };
    }
  }

  function networkPresentation(domains) {
    const states = DOMAINS.map((name) => domains[name].state);
    const unavailable = states.filter((state) => state === "unavailable").length;
    const legacy = states.filter((state) => state === "legacy-fallback").length;
    const api = states.filter((state) => ["normal", "degraded", "stale"].includes(state)).length;
    let overall = "normal";
    if (unavailable) overall = api || legacy ? "partial-unavailable" : "unavailable";
    else if (legacy) overall = api ? "partial-unavailable" : "legacy-fallback";
    else if (states.includes("stale")) overall = "stale";
    else if (states.includes("degraded")) overall = "degraded";
    return { overall, domains };
  }

  async function resolveNetworkSnapshot(payload, fallbacks, apiErrors = {}) {
    const values = normalizeNetworkOverview(payload);
    const warnings = (payload && Array.isArray(payload.warnings)) ? payload.warnings : [];
    const entries = await Promise.all(DOMAINS.map(async (name) => [
      name, {
        ...await resolveNetworkDomain(values[name], fallbacks[name], name),
        apiError: networkErrorInfo(apiErrors[name] || warnings.find((item) => item && item.domain === name && item.code))
      }
    ]));
    return networkPresentation(Object.fromEntries(entries));
  }

  async function loadNetworkDomains(session, clients, fallbacks) {
    if (!await session.canRead()) {
      const snapshot = await resolveNetworkSnapshot(null, fallbacks);
      return { ...snapshot, overall: snapshot.overall === "legacy-fallback" ? "unauthenticated" : snapshot.overall };
    }
    const errors = {};
    const results = await Promise.all(DOMAINS.map(async (name) => {
      try {
        return [name, await clients[name]()];
      } catch (error) {
        if (isAuthError(error)) session.expired();
        errors[name] = error;
        return [name, null];
      }
    }));
    return resolveNetworkSnapshot(Object.fromEntries(results), fallbacks, errors);
  }

  function createNetworkSession(fetchAuthStatus, now = () => Date.now(), retryMs = 45000) {
    let authenticated = null;
    let nextCheckAt = -Infinity;
    let pending = null;
    let revision = 0;
    return {
      async canRead() {
        if (now() < nextCheckAt && authenticated !== null) return authenticated;
        if (!pending) {
          const current = revision;
          const request = Promise.resolve().then(fetchAuthStatus)
            .then((result) => {
              if (revision !== current) return;
              if (result && result.transient === true) {
                authenticated = authenticated === true;
                nextCheckAt = now() + Math.min(retryMs, 5000);
              } else {
                authenticated = !!(result && result.authenticated === true);
                nextCheckAt = now() + retryMs;
              }
            })
            .catch(() => {
              if (revision === current) {
                authenticated = authenticated === true;
                nextCheckAt = now() + Math.min(retryMs, 5000);
              }
            })
            .finally(() => { if (pending === request) pending = null; });
          pending = request;
        }
        await pending;
        return authenticated === true;
      },
      expired() { revision++; pending = null; authenticated = false; nextCheckAt = now() + retryMs; },
      invalidate() { revision++; pending = null; authenticated = null; nextCheckAt = -Infinity; }
    };
  }

  function isAuthError(error) { return error && (error.status === 401 || error.status === 403); }

  return {
    deviceStatus, networkDomainState, networkErrorInfo, normalizeNetworkOverview, mergeNetworkDevices,
    overlayTopologyDeviceStatus, topologyNetworkTargets, networkIssueNotice, renderNetworkIssue,
    mergeIspInventory, mergeLegacyIspInventory, controlNetworkTargets, resolveNetworkDomain, resolveNetworkSnapshot, networkPresentation,
    createNetworkSession, loadNetworkDomains, isAuthError
  };
}));
