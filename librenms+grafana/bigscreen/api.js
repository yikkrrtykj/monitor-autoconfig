;(function () {
  'use strict';

  // Data layer: everything that talks to Prometheus / the topology endpoints,
  // plus the ISP-name/bandwidth config helpers those queries need. Pure
  // formatting/parsing lives in utils.js; DOM rendering stays in app.js.
  const utils = (typeof module !== 'undefined' && module.exports)
    ? require('./utils.js')
    : window.BSUtils;
  const {
    metricName, escapeRegex, escapeLabel, uniqueNames, parseIspBandwidthConfig
  } = utils;

  const config = (typeof window !== 'undefined' && window.BIGSCREEN_CONFIG) || {};
  const queries = (typeof window !== 'undefined' && window.BIGSCREEN_QUERIES) || {};
  const infraPingJobs = queries.infraPingJobs || "infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping";

  function prometheusBaseUrl() {
    if (config.prometheusBaseUrl) {
      return config.prometheusBaseUrl.replace(/\/$/, "");
    }
    return "/prometheus";
  }

  function rangeWindow() {
    const end = Math.floor(Date.now() / 1000);
    const start = end - 15 * 60;
    return { start, end, step: 10 };
  }

  function fetchWithTimeout(url, options, timeoutMs = 15000) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(id));
  }

  async function prometheusQuery(query) {
    const url = `${prometheusBaseUrl()}/api/v1/query?query=${encodeURIComponent(query)}`;
    const response = await fetchWithTimeout(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Prometheus HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.status !== "success") {
      throw new Error("Prometheus query failed");
    }
    return payload.data.result
      .map((item) => ({
        name: metricName(item.metric),
        value: Number(item.value[1]),
        metric: item.metric || {}
      }))
      .filter((item) => Number.isFinite(item.value))
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  async function prometheusInstant(query) {
    const url = `${prometheusBaseUrl()}/api/v1/query?query=${encodeURIComponent(query)}`;
    const response = await fetchWithTimeout(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Prometheus HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.status !== "success") {
      throw new Error("Prometheus query failed");
    }
    return payload.data.result
      .map((item) => ({
        metric: item.metric || {},
        value: Number(item.value[1])
      }))
      .filter((item) => Number.isFinite(item.value));
  }

  async function prometheusRangeFor(query, window, nameGetter = metricName) {
    const params = new URLSearchParams({
      query,
      ...Object.fromEntries(Object.entries(window).map(([key, value]) => [key, String(value)]))
    });
    const response = await fetchWithTimeout(`${prometheusBaseUrl()}/api/v1/query_range?${params.toString()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Prometheus range HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.status !== "success") {
      throw new Error("Prometheus range query failed");
    }
    return payload.data.result
      .map((item) => ({
        name: nameGetter(item.metric || {}),
        metric: item.metric || {},
        values: item.values
          .map(([timestamp, value]) => ({ t: Number(timestamp), v: Number(value) }))
          .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v))
      }))
      .filter((item) => item.values.length)
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  async function prometheusRange(query, nameGetter = metricName) {
    return prometheusRangeFor(query, rangeWindow(), nameGetter);
  }

  // Incremental range cache: the first call fetches the whole 15-minute window;
  // every subsequent call only asks Prometheus for points newer than what we
  // already hold, then merges + trims to the sliding window. Historical samples
  // are immutable, so this is exact -- it just avoids re-downloading ~90 points
  // per series each 5s tick. Safe for rate()/avg gauges since each returned
  // point is self-contained (Prometheus looks back over its own window
  // server-side).
  const rangeCache = new Map();

  function invalidateRangeCache() {
    rangeCache.clear();
  }

  async function prometheusRangeCached(query, nameGetter = metricName) {
    const win = rangeWindow();
    const cacheKey = `${query}|${win.step}`;
    const cached = rangeCache.get(cacheKey);

    let fetchWin = win;
    let existingMap = new Map();

    if (cached && cached.fetchedUpTo > win.start) {
      const fetchStart = cached.fetchedUpTo + win.step;
      if (fetchStart > win.end) {
        // No new sample is due yet -- trim the stale head and return as-is.
        const result = [];
        cached.seriesMap.forEach((item) => {
          const trimmed = item.values.filter((point) => point.t >= win.start);
          if (trimmed.length) result.push({ ...item, values: trimmed });
        });
        return result.sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
      }
      fetchWin = { start: fetchStart, end: win.end, step: win.step };
      existingMap = cached.seriesMap;
    }

    const newSeries = await prometheusRangeFor(query, fetchWin, nameGetter);

    // Advance the watermark to the newest real sample we actually received, not
    // the requested end -- a point still in flight (scrape lag) is then re-tried
    // next tick instead of skipped. Fall back to the requested end when the
    // window came back empty so a dead query can't pin us in place forever.
    let maxNewT = 0;
    newSeries.forEach((item) => {
      const last = item.values.length ? item.values[item.values.length - 1].t : 0;
      if (last > maxNewT) maxNewT = last;
    });
    const fetchedUpTo = maxNewT > 0 ? Math.max(maxNewT, cached ? cached.fetchedUpTo : 0) : fetchWin.end;

    const mergedMap = new Map(existingMap);
    newSeries.forEach((item) => {
      const prev = mergedMap.get(item.name);
      if (prev) {
        const lastT = prev.values.length ? prev.values[prev.values.length - 1].t : 0;
        const appended = prev.values.concat(item.values.filter((point) => point.t > lastT));
        mergedMap.set(item.name, { ...item, values: appended });
      } else {
        mergedMap.set(item.name, { ...item });
      }
    });

    // Trim every series to the sliding window; drop series that aged out.
    mergedMap.forEach((item, name) => {
      const trimmed = item.values.filter((point) => point.t >= win.start);
      if (trimmed.length) {
        mergedMap.set(name, { ...item, values: trimmed });
      } else {
        mergedMap.delete(name);
      }
    });

    rangeCache.set(cacheKey, { fetchedUpTo, seriesMap: mergedMap });

    return Array.from(mergedMap.values())
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  // Infra ping instances that have actually been online at least once recently
  // ("deployed"). Lets the overview hide configured-but-never-online targets
  // (e.g. a DIST_SWITCH_PING=SW:.11-30 range where only a few switches exist)
  // while keeping deployed-but-currently-down ones (they still show red).
  // Mirrors DEVICE_DOWN_REQUIRE_SEEN_UP on the alerting side.
  function activeInfraPingQuery() {
    const jobs = "infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping";
    return `max by (instance) (max_over_time(probe_success{job=~"${jobs}"}[6h])) >= 1`;
  }

  function activeSeriesNames(items) {
    const names = new Set();
    items.forEach((item) => {
      const metric = item.metric || {};
      [metricName(metric), metric.target_ip, metric.display_name, metric.instance]
        .map((value) => String(value || "").trim())
        .filter(Boolean)
        .forEach((value) => names.add(value));
    });
    return names;
  }

  function looksLikeIp(value) {
    return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(String(value || ""));
  }

  // HA 设备的 Member0/Member1/Member2 是区分物理成员所必需的设备自带名称，
  // 必须保留。只过滤完全没有识别价值的出厂通用名。
  const GENERIC_SYSNAME_RE = /^(switch|router|firewall|amnesiac)$/i;

  function isMeaningfulSysName(value) {
    return Boolean(value) && !looksLikeIp(value) && !GENERIC_SYSNAME_RE.test(value);
  }

  function bestSysName(metric) {
    const candidates = [
      metric.sysName,
      metric.system_name,
      metric.systemName,
      metric.snmp_sysName
    ];
    for (const candidate of candidates) {
      const value = String(candidate || "").trim();
      if (isMeaningfulSysName(value)) return value;
    }
    return "";
  }

  let infraNameCache = null;
  let infraNameCachedAt = 0;
  const INFRA_NAME_STORAGE_KEY = "bigscreen.infraDeviceNames.v1";

  function readStoredInfraDeviceNames() {
    try {
      const storage = typeof window !== "undefined" ? window.localStorage : null;
      if (!storage) return new Map();
      const entries = JSON.parse(storage.getItem(INFRA_NAME_STORAGE_KEY) || "[]");
      if (!Array.isArray(entries)) return new Map();
      // isMeaningfulSysName 同时清掉历史版本缓存下来的 IP/通用占位名。
      return new Map(entries.filter(([key, value]) => key && isMeaningfulSysName(String(value || ""))));
    } catch (error) {
      return new Map();
    }
  }

  function writeStoredInfraDeviceNames(map) {
    try {
      const storage = typeof window !== "undefined" ? window.localStorage : null;
      if (!storage) return;
      storage.setItem(INFRA_NAME_STORAGE_KEY, JSON.stringify(Array.from(map.entries()).slice(-500)));
    } catch (error) {
      // Storage can be disabled in kiosk browsers; the in-memory cache is enough.
    }
  }

  function rememberInfraName(map, metric, name) {
    if (!isMeaningfulSysName(String(name || ""))) return;
    [metric.target_ip, metric.instance, metric.display_name]
      .map((value) => String(value || "").trim())
      .filter(Boolean)
      .forEach((key) => map.set(key, name));
  }

  async function fetchInfraDeviceNames() {
    const now = Date.now();
    if (infraNameCache && now - infraNameCachedAt < 15000) {
      return infraNameCache;
    }

    const map = new Map(infraNameCache || readStoredInfraDeviceNames());
    const queries = [
      'max_over_time(sysName{job=~"infra-switch-snmp|infra-fw-snmp|infra-fw-unit-snmp"}[12h])',
      'sysName{job=~"infra-switch-snmp|infra-fw-snmp|infra-fw-unit-snmp"}'
    ];
    for (const query of queries) {
      try {
        const items = await prometheusInstant(query);
        items.forEach((item) => {
          const metric = item.metric || {};
          rememberInfraName(map, metric, bestSysName(metric));
        });
      } catch (error) {
        // Older deployments may not have sysName in the lightweight SNMP module yet.
      }
    }
    try {
      const units = await prometheusInstant('last_over_time(up{job="infra-fw-unit-snmp"}[25m])');
      units
        .slice()
        .sort((a, b) => String(a.metric.target_ip || a.metric.instance || "")
          .localeCompare(String(b.metric.target_ip || b.metric.instance || ""), "zh-CN", { numeric: true }))
        .forEach((item, index) => {
          const metric = item.metric || {};
          const keys = [metric.target_ip, metric.instance, metric.display_name]
            .map((value) => String(value || "").trim())
            .filter(Boolean);
          if (keys.some((key) => map.has(key))) return;
          const configuredName = String(metric.display_name || "").trim();
          const fallbackName = isMeaningfulSysName(configuredName) ? configuredName : `防火墙成员${index + 1}`;
          rememberInfraName(map, metric, fallbackName);
        });
    } catch (error) {
      // Unit targets are optional; deployments without FireCluster keep the old name map.
    }
    infraNameCache = map;
    infraNameCachedAt = now;
    if (map.size) writeStoredInfraDeviceNames(map);
    return map;
  }

  function renameWithInfraMap(item, nameMap) {
    const metric = item.metric || {};
    const mapped = nameMap.get(metric.target_ip) || nameMap.get(metric.instance) || nameMap.get(item.name);
    if (!mapped || mapped === item.name) {
      return item;
    }
    return {
      ...item,
      originalName: item.originalName || item.name,
      name: mapped
    };
  }

  function renameListWithInfraMap(list, nameMap) {
    return list.map((item) => renameWithInfraMap(item, nameMap));
  }

  function filterSeriesByNames(seriesList, names) {
    return seriesList.filter((item) => names.has(item.name));
  }

  function isIspAutoDiscoveryEnabled() {
    return ["1", "true", "yes", "on"].includes(String(config.ispAutoDiscovery || "").trim().toLowerCase());
  }

  function wanFilterPattern() {
    // 以数字结尾的关键词按边界匹配：WatchGuard 这类防火墙 SNMP 只报 eth0/eth1
    // 物理名，运维只能填 eth1 来圈 WAN 口，不能让它顺带命中 eth10~eth15。
    // 不以数字结尾的关键词维持包含匹配（WAN 仍命中 WAN1）。
    return String(config.wanIfFilter || "telecom,telcom,unicom,isp,wan")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean)
      .map((name) => (/\d$/.test(name) ? `${escapeRegex(name)}(?:[^0-9]|$)` : escapeRegex(name)))
      .join("|") || "telecom|telcom|unicom|isp|wan";
  }

  function ispDiscoveryQuery() {
    const pattern = wanFilterPattern();
    return `group by (ifAlias,ifIndex) (ifHCInOctets{job="firewall-snmp",ifAlias=~".+",ifAlias=~"(?i).*(${pattern}).*"}) or group by (ifName,ifIndex) (ifHCInOctets{job="firewall-snmp",ifAlias="",ifName=~".+",ifName=~"(?i).*(${pattern}).*"}) or group by (ifDescr,ifIndex) (ifHCInOctets{job="firewall-snmp",ifAlias="",ifName="",ifDescr=~".+",ifDescr=~"(?i).*(${pattern}).*"})`;
  }

  // 运维显式填写的 ISP 名字（不注入默认）。未填则为空数组。
  function getConfiguredIspNames() {
    return uniqueNames(String(config.ispNames || "")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean))
      .slice(0, 4);
  }

  // 非自动发现 / 兜底时使用：有显式名字用显式的，否则回退 ISP1,ISP2 默认（保持旧行为）。
  function getIspNames() {
    const configured = getConfiguredIspNames();
    return configured.length ? configured : ["ISP1", "ISP2"];
  }

  let ispNamesCache = null;
  let ispNamesCachedAt = 0;

  async function fetchIspNames() {
    const configured = getConfiguredIspNames();
    if (!isIspAutoDiscoveryEnabled()) {
      return getIspNames();
    }

    const now = Date.now();
    if (ispNamesCache && now - ispNamesCachedAt < 60000) {
      return ispNamesCache;
    }

    try {
      const discovered = await prometheusInstant(ispDiscoveryQuery());
      discovered.sort((a, b) => {
        const aIndex = Number(a.metric.ifIndex);
        const bIndex = Number(b.metric.ifIndex);
        if (Number.isFinite(aIndex) && Number.isFinite(bIndex) && aIndex !== bIndex) return aIndex - bIndex;
        const aName = a.metric.ifAlias || a.metric.ifName || a.metric.ifDescr || "";
        const bName = b.metric.ifAlias || b.metric.ifName || b.metric.ifDescr || "";
        return aName.localeCompare(bName, "zh-CN", { numeric: true });
      });
      const discoveredNames = uniqueNames(discovered.map((item) => item.metric.ifAlias || item.metric.ifName || item.metric.ifDescr));
      // 显式名字 + 发现到的口合并；没填显式名字就只显示发现到的（换场地零改配置）。
      const names = uniqueNames([...configured, ...discoveredNames]).slice(0, 4);
      ispNamesCache = names.length ? names : getIspNames();
      ispNamesCachedAt = now;
      return ispNamesCache;
    } catch (error) {
      console.warn("ISP discovery failed", error);
      return getIspNames();
    }
  }

  function ispTrafficQuery(metric, name) {
    const label = escapeLabel(name);
    return `sum(rate(${metric}{job="firewall-snmp",ifAlias="${label}"}[1m]) or rate(${metric}{job="firewall-snmp",ifAlias="",ifName="${label}"}[1m]) or rate(${metric}{job="firewall-snmp",ifAlias="",ifName="",ifDescr="${label}"}[1m])) * 8`;
  }

  async function fetchIspTraffic() {
    const names = await fetchIspNames();
    const settled = await Promise.allSettled(names.map(async (name) => {
      const [download, upload] = await Promise.all([
        prometheusRangeCached(ispTrafficQuery("ifHCInOctets", name)),
        prometheusRangeCached(ispTrafficQuery("ifHCOutOctets", name))
      ]);
      return {
        name,
        download: { name: "下载", color: "#73d17a", values: download[0] ? download[0].values : [] },
        upload: { name: "上传", color: "#5b8ff9", values: upload[0] ? upload[0].values : [] }
      };
    }));
    return settled.filter((r) => r.status === "fulfilled").map((r) => r.value);
  }

  function ispCapacityBps(name, direction, index = -1) {
    const cfg = parseIspBandwidthConfig(config.ispMaxBandwidthMbps);
    const entry = cfg.perIsp[name] || cfg.ordered[index] || cfg.default;
    const mbps = direction === "in" ? entry.down : entry.up;
    return Math.max(1, Number(mbps) || 1000) * 1000 * 1000;
  }

  function ispChartMaxBps(name, index = -1) {
    return Math.max(ispCapacityBps(name, "in", index), ispCapacityBps(name, "out", index));
  }

  async function fetchTopologyTargets() {
    const jobs = ["infra-isp-ping", "infra-fw-ping", "infra-core-ping", "infra-dist-ping", "infra-srv-ping"];
    const filter = jobs.join("|");
    const [success, latency, unitStatus, nameMap] = await Promise.all([
      prometheusInstant(`probe_success{job=~"${filter}"}`),
      prometheusInstant(`probe_icmp_duration_seconds{job=~"${filter}",phase="rtt"}`),
      // Keep both HA members visible through an occasional missed/late SNMP scrape.
      // The last sample still carries the current target labels and up/down value.
      prometheusInstant('last_over_time(up{job="infra-fw-unit-snmp"}[25m])'),
      fetchInfraDeviceNames()
    ]);
    const map = new Map();
    success.forEach((item) => {
      const key = `${item.metric.job}|${item.metric.target_ip || item.metric.instance}`;
      const displayName = nameMap.get(item.metric.target_ip) || nameMap.get(item.metric.instance) || item.metric.display_name || item.metric.instance;
      map.set(key, {
        job: item.metric.job,
        instance: item.metric.instance || item.metric.target_ip,
        targetIp: item.metric.target_ip || item.metric.instance,
        wanIp: item.metric.wan_ip || "",
        displayName,
        success: item.value >= 1,
        latency: null
      });
    });
    latency.forEach((item) => {
      const key = `${item.metric.job}|${item.metric.target_ip || item.metric.instance}`;
      const node = map.get(key);
      if (node) node.latency = item.value;
    });
    unitStatus
      .slice()
      .sort((a, b) => String(a.metric.target_ip || a.metric.instance || "")
        .localeCompare(String(b.metric.target_ip || b.metric.instance || ""), "zh-CN", { numeric: true }))
      .forEach((item, index) => {
        const metric = item.metric || {};
        const targetIp = metric.target_ip || metric.instance;
        if (!targetIp) return;
        const key = `infra-fw-unit-snmp|${targetIp}`;
        const configuredName = String(metric.display_name || "").trim();
        const displayName = nameMap.get(metric.target_ip) || nameMap.get(metric.instance) ||
          (isMeaningfulSysName(configuredName) ? configuredName : `防火墙成员${index + 1}`);
        map.set(key, {
          job: "infra-fw-unit-snmp",
          instance: metric.instance || targetIp,
          targetIp,
          displayName,
          success: item.value >= 1,
          latency: null
        });
      });
    return Array.from(map.values());
  }

  async function fetchTopologyEdges() {
    try {
      const response = await fetchWithTimeout("/topology/edges.json", { cache: "no-store" });
      if (!response.ok) return [];
      const data = await response.json();
      return Array.isArray(data) ? data : [];
    } catch (error) {
      return [];
    }
  }

  async function fetchRuntimeStatus() {
    try {
      const response = await fetchWithTimeout("/player-targets/status", { cache: "no-store" }, 5000);
      if (!response.ok) {
        return { ok: false, error: `HTTP ${response.status}` };
      }
      return await response.json();
    } catch (error) {
      return { ok: false, error: error.message || "runtime status unavailable" };
    }
  }

  async function platformApi(path, options = {}) {
    const response = await fetchWithTimeout(`/platform-api${path}`, {
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options
    }, options.timeoutMs || 15000);
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const error = new Error((payload && payload.error) || `Platform API HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return response.json();
  }

  async function fetchPlatformAuthStatus() {
    try {
      return await platformApi("/auth/status", { timeoutMs: 5000 });
    } catch (error) {
      // No HTTP status means a transport failure (fetch rejected) -- the proxy is
      // briefly unreachable, e.g. bigscreen restarting during 应用配置. Flag it as
      // transient so the UI can hold the current view instead of bouncing to login.
      const transient = error.status == null;
      return { ok: false, enabled: true, authenticated: false, transient, error: error.message || "auth unavailable" };
    }
  }

  function loginPlatformAuth(username, password) {
    return platformApi("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password })
    });
  }

  function changePlatformPassword(currentPassword, newPassword, confirmPassword) {
    return platformApi("/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ currentPassword, newPassword, confirmPassword })
    });
  }

  function logoutPlatformAuth() {
    return platformApi("/auth/logout", { method: "POST", body: JSON.stringify({}) });
  }

  async function fetchPlatformConfig() {
    try {
      return await platformApi("/config", { timeoutMs: 5000 });
    } catch (error) {
      return { ok: false, error: error.message || "platform config unavailable" };
    }
  }

  async function fetchApplyStatus(operationId) {
    try {
      const query = new URLSearchParams({ operationId: String(operationId || "") });
      return await platformApi(`/config/apply-status?${query.toString()}`, { timeoutMs: 5000 });
    } catch (error) {
      return { ok: false, state: "unavailable", operationId, error: error.message || "apply status unavailable" };
    }
  }

  function postPlatform(path, payload, options) {
    return platformApi(path, { method: "POST", body: JSON.stringify(payload || {}), ...(options || {}) });
  }

  async function fetchIperfStatus() {
    try {
      return await platformApi("/network/iperf3/status", { timeoutMs: 3000 });
    } catch (error) {
      return { ok: false, state: "unavailable", error: error.message || "测速状态不可用" };
    }
  }

  async function fetchRetirePending() {
    try {
      return await platformApi("/network/retire/pending", { timeoutMs: 10000 });
    } catch (error) {
      return { ok: false, pending: [], error: error.message || "待删除列表不可用" };
    }
  }

  function patchPlatform(path, payload) {
    return platformApi(path, { method: "PATCH", body: JSON.stringify(payload || {}) });
  }

  async function fetchIncidents() {
    try {
      return await platformApi("/incidents", { timeoutMs: 5000 });
    } catch (error) {
      return { ok: false, incidents: [], error: error.message || "incident store unavailable" };
    }
  }

  async function fetchDeliveryManifest() {
    try {
      return await platformApi("/delivery/manifest", { timeoutMs: 5000 });
    } catch (error) {
      return { ok: false, images: [], files: [], commands: [], error: error.message || "delivery manifest unavailable" };
    }
  }

  function fetchDhcpDashboard(force = false) {
    const query = force ? "?force=1" : "";
    return platformApi(`/network/dhcp${query}`, { timeoutMs: 30000 });
  }

  function testDhcpConnection() {
    return platformApi("/network/dhcp/test", {
      method: "POST",
      body: JSON.stringify({}),
      timeoutMs: 30000
    });
  }

  async function fetchDhcpSettings() {
    try {
      return await platformApi("/network/dhcp/settings", { timeoutMs: 5000 });
    } catch (error) {
      return { ok: false, error: error.message || "Telnet settings unavailable" };
    }
  }

  function saveDhcpSettings(payload) {
    return platformApi("/network/dhcp/settings", {
      method: "POST",
      body: JSON.stringify(payload || {}),
      timeoutMs: 10000
    });
  }

  const ns = {
    prometheusBaseUrl,
    rangeWindow,
    fetchWithTimeout,
    prometheusQuery,
    prometheusInstant,
    prometheusRangeFor,
    prometheusRange,
    prometheusRangeCached,
    invalidateRangeCache,
    activeInfraPingQuery,
    activeSeriesNames,
    filterSeriesByNames,
    isIspAutoDiscoveryEnabled,
    wanFilterPattern,
    ispDiscoveryQuery,
    getConfiguredIspNames,
    getIspNames,
    fetchIspNames,
    ispTrafficQuery,
    fetchIspTraffic,
    ispCapacityBps,
    ispChartMaxBps,
    fetchInfraDeviceNames,
    renameListWithInfraMap,
    fetchTopologyTargets,
    fetchTopologyEdges,
    fetchRuntimeStatus,
    fetchPlatformAuthStatus,
    loginPlatformAuth,
    changePlatformPassword,
    logoutPlatformAuth,
    fetchPlatformConfig,
    fetchApplyStatus,
    postPlatform,
    fetchIperfStatus,
    fetchRetirePending,
    patchPlatform,
    fetchIncidents,
    fetchDeliveryManifest,
    fetchDhcpDashboard,
    testDhcpConnection,
    fetchDhcpSettings,
    saveDhcpSettings
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSApi = ns;
  }
}());
