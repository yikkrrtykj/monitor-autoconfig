;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const dnsKey = "custom-dns:";
  const stateLabels = Object.freeze({
    DNS_PENDING: "等待解析", DNS_OK: "正常", DNS_ERROR: "解析失败",
    DNS_STALE: "解析已过期", DNS_POLICY_BLOCKED: "解析地址未获授权"
  });

  function ipv4(value) {
    return typeof value === "string" && /^(?:0|[1-9]\d{0,2})(?:\.(?:0|[1-9]\d{0,2})){3}$/.test(value) &&
      value.split(".").every((part) => Number(part) <= 255);
  }

  function addressInput(input) {
    const value = input.trim();
    if (ipv4(value)) return { kind: "ipv4", address: value };
    if (!value || [...value].length > 254 || !/^[\p{L}\p{M}\p{N}.\-\u3002\uff0e\uff61]+$/u.test(value)) return null;
    let address;
    try {
      // Python's IDNA codec uses Nameprep. Apply its common mappings and
      // let the write API make the final decision for international names.
      const normalized = value.normalize("NFKC").toLowerCase().replace(/ß/g, "ss").replace(/ς/g, "σ");
      const fullname = normalized.replace(/[。．｡]/g, ".").replace(/\.$/, "");
      address = /^[\x00-\x7f]+$/.test(fullname) ? fullname :
        new URL("http://" + fullname + ".invalid").hostname.slice(0, -8);
    } catch { return null; }
    const parts = address.split(".");
    if (address.length > 253 || parts.length < 2 || /^[0-9.]+$/.test(address) ||
        ["local", "localhost"].includes(parts.at(-1)) ||
        parts.some((label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return null;
    return { kind: "dns", address };
  }

  function privateCidrs(input) {
    const values = input.trim().split(/[\s,]+/).filter(Boolean);
    if (values.length > 16) return null;
    for (const value of values) {
      const [ip, bits, extra] = value.split("/");
      if (!ipv4(ip) || !/^(?:[1-9]|[12]\d|3[0-2])$/.test(bits) || extra !== undefined) return null;
      const [a, b] = ip.split(".").map(Number);
      const min = a === 10 ? 8 : a === 172 && b >= 16 && b <= 31 ? 12 : a === 192 && b === 168 ? 16 : 33;
      const number = ip.split(".").reduce((n, octet) => n * 256 + Number(octet), 0);
      if (Number(bits) < min || number % 2 ** (32 - Number(bits)) !== 0) return null;
    }
    return [...new Set(values)].sort();
  }

  function editPayload(name, parsed, group, cidrs = []) {
    const fallback = parsed.address.length <= 80 ? parsed.address : parsed.address.slice(0, 79) + "…";
    const displayName = name.trim() || fallback;
    if ([...displayName].length > 80 || new TextEncoder().encode(displayName).length > 240 || /\p{C}/u.test(displayName)) {
      return null;
    }
    return parsed.kind === "ipv4" ? { name: displayName, ip: parsed.address, group } :
      { name: displayName, kind: "dns", address: parsed.address, group, privateCidrs: cidrs };
  }

  function endpoint(id, ip) {
    return { job: "custom-ping", custom_id: id, instance: id, target_ip: ip };
  }

  function target(row) {
    const { id, address, resolvedIp, name, group, resolutionGeneration, expiresAt } = row;
    return { key: dnsKey + id, metric: endpoint(id, resolvedIp || ""), job: "custom-ping",
      category: "custom", group: "Custom", interval: 2, lossWindow: 60, dns: true, address,
      name: address || "域名目标", friendlyName: name === address ? "" : name || "", subtitle: name || "",
      ip: resolvedIp || "", customGroup: group, resolutionGeneration, expiresAt };
  }

  function parseTarget(key) {
    const id = key.startsWith(dnsKey) ? key.slice(dnsKey.length) : "";
    return uuid.test(id) ? target({ id }) : null;
  }

  const generation = (v) => Number.isSafeInteger(v) && v > 0;
  const stamp = (v) => Number.isFinite(v) && v >= 0;

  // DNS observed rows are independently confirmed by D2 even if a neighbor's
  // publication is pending. Join both authoritative views and the actual SD row.
  function verifiedEndpoint(catalog, discovery, row, now) {
    const observed = discovery?.dnsTargets?.find((item) => item.id === row.id);
    const active = discovery?.activeTargets?.find((item) => item.id === row.id);
    if (!catalog || catalog.complete === false || catalog.dnsEnabled !== true ||
        catalog.manifestRevision !== catalog.revision || discovery?.revision !== catalog.revision ||
        !row.enabled || row.dnsStatus !== "DNS_OK" || !ipv4(row.resolvedIp) ||
        !stamp(row.expiresAt) || now >= row.expiresAt || !generation(row.resolutionGeneration) ||
        observed?.dnsStatus !== "DNS_OK" || observed.discoveryStatus !== "DISCOVERED" ||
        observed.withdrawalPending !== false || observed.resolvedIp !== row.resolvedIp ||
        observed.resolutionGeneration !== row.resolutionGeneration || observed.discoveredGeneration !== row.resolutionGeneration ||
        active?.ip !== row.resolvedIp || active.revision !== row.revision ||
        active.resolutionGeneration !== row.resolutionGeneration ||
        !stamp(row.sampleNotBefore) || observed.sampleNotBefore !== row.sampleNotBefore || observed.sampleNotBefore >= now) return null;
    return { ...target(row), sampleNotBefore: observed.sampleNotBefore, pingStatus: observed.pingStatus };
  }

  function presentation(catalog, discovery, row, samples, settings, notice, now) {
    const item = target(row);
    const observed = discovery?.dnsTargets?.find((entry) => entry.id === row.id);
    const expired = row.dnsStatus === "DNS_OK" && (!stamp(row.expiresAt) || row.expiresAt <= now);
    const dnsStatus = expired ? "DNS_STALE" : row.dnsStatus;
    const dnsLabel = "DNS：" + (stateLabels[dnsStatus] || "暂不可用") +
      (Number.isInteger(row.aCount) && row.aCount > 0 && row.aCount <= 64 ? " · " + row.aCount + " 个 A 记录" : "");
    const withdrawalPending = observed?.withdrawalPending === true;
    const ready = verifiedEndpoint(catalog, discovery, row, now);
    const reading = ready && samples?.get(item.key);
    const common = { ...item, dnsLabel: dnsLabel + (ready ? "，当前监控 1 个" : ""), dnsStatus,
      sampleNotBefore: ready?.sampleNotBefore ?? null, withdrawalPending };
    const sync = withdrawalPending ? "等待停止旧探测" :
      dnsStatus !== "DNS_OK" ? (stateLabels[dnsStatus] || "等待解析") :
      ready ? "等待新样本" : "等待新地址发现";
    if (!notice && !expired && ready && reading && reading.sourceAt > ready.sampleNotBefore &&
        reading.sourceAt <= now && now - reading.sourceAt <= 10 &&
        reading.status === ready.pingStatus && ["UP", "DOWN"].includes(reading.status)) {
      const status = reading.status === "DOWN" ? "DOWN" :
        reading.loss.loss >= settings.lossThreshold || reading.rtt >= settings.rttThreshold ? "DEGRADED" : "UP";
      return { ...common, sync: "已发现", status, rtt: reading.rtt, loss: reading.loss, sourceAt: reading.sourceAt,
        reason: "" };
    }
    const reason = notice || (row.dnsError === "timeout" ? "DNS 查询超时" : "");
    return { ...common, sync, status: "UNKNOWN", rtt: null, sourceAt: null, loss: model.lossStats(null, null, 60, 2), reason };
  }

  const api = { uuid, ipv4, addressInput, privateCidrs, editPayload, endpoint, target, parseTarget,
    generation, stamp, verifiedEndpoint, presentation };
  if (common) module.exports = api;
  else window.BSPingCustomDNS = api;
})();
