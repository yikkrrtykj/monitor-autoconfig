;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const dns = common ? require("./custom-dns.js") : window.BSPingCustomDNS;
  const groups = Object.freeze(["Remote", "Server", "Other"]);
  const revision = (value) => Number.isSafeInteger(value) && value >= 0;
  const publication = (value) => revision(value.revision) &&
    (value.publication === "published" && value.publishedRevision === value.revision ||
     value.publication === "pending" && value.publishedRevision === null);
  const endpoint = ({ id, ip }) => ({ job: "custom-ping", custom_id: id, instance: id, target_ip: ip });
  const validEndpoint = (row) => !!row && model.customMetric(endpoint(row)) && revision(row.revision) && row.revision > 0;

  function validTarget(row) {
    const identity = row?.kind === "dns"
      ? dns.uuid.test(row.id || "") && typeof row.address === "string" &&
        dns.addressInput(row.address)?.address === row.address && Array.isArray(row.privateCidrs) &&
        revision(row.endpointRevision) && row.endpointRevision > 0 && row.endpointRevision <= row.revision &&
        revision(row.revision)
      : validEndpoint(row);
    return identity && typeof row.name === "string" && row.name.length > 0 && groups.includes(row.group) &&
      typeof row.enabled === "boolean" && Number.isFinite(Date.parse(row.updatedAt));
  }

  function validDiscovery(value) {
    return value?.ok === true && revision(value.revision) &&
      (value.dnsTargets === undefined || Array.isArray(value.dnsTargets) && value.dnsTargets.length <= 300 &&
        value.dnsTargets.every((row) => row && dns.uuid.test(row.id || "")) &&
        new Set(value.dnsTargets.map(({ id }) => id)).size === value.dnsTargets.length) &&
      (value.publishedRevision === null || value.publishedRevision === value.revision) &&
      (value.discoveredRevision === null || value.discoveredRevision === value.revision) &&
      Array.isArray(value.activeTargets) && value.activeTargets.length <= 100 && value.activeTargets.every(validEndpoint) &&
      new Set(value.activeTargets.map(({ id }) => id)).size === value.activeTargets.length;
  }

  function createState() {
    let current = null;
    let floor = 0;

    function read(value) {
      if (value?.ok !== true || !publication(value) || !Array.isArray(value.targets) || value.targets.length > 300 ||
          (value.manifestRevision !== undefined && value.manifestRevision !== value.revision) ||
          !value.targets.every((row) => validTarget(row) && row.revision <= value.revision &&
            (row.kind !== "dns" || value.manifestRevision === value.revision)) ||
          new Set(value.targets.map(({ id }) => id)).size !== value.targets.length) {
        throw new Error("自定义清单响应无效");
      }
      if (value.targets.filter((row) => row.enabled).length > 100) throw new Error("自定义清单启用数量无效");
      if (value.revision < floor) return false;
      floor = value.revision;
      current = { ...value, complete: true };
      return true;
    }

    function save(receipt) {
      if (!publication(receipt) || !revision(receipt.savedRevision) || receipt.savedRevision < 1 ||
          receipt.savedRevision > receipt.revision || typeof receipt.id !== "string" ||
          receipt.target !== null && (!validTarget(receipt.target) ||
            receipt.target.id !== receipt.id || receipt.target.revision !== receipt.savedRevision)) {
        throw new Error("自定义目标回执无效");
      }
      if (receipt.revision > receipt.savedRevision) {
        // Idempotent replay: the operation applied at savedRevision, but the
        // global manifest has since advanced. The receipt only describes the
        // older delta, so it must never be merged or relabelled as the newer
        // authoritative list. Keep the last known rows but require a full read.
        floor = Math.max(floor, receipt.revision);
        if (current?.complete && current.revision >= receipt.revision) return "current";
        if (current) current = { ...current, complete: false };
        return "reread";
      }
      if (receipt.savedRevision < floor || current?.complete && receipt.savedRevision === floor) {
        floor = Math.max(floor, receipt.revision);
        return "current";
      }
      floor = Math.max(floor, receipt.revision);
      const rows = new Map((current?.targets || []).map((row) => [row.id, row]));
      if (receipt.target === null) rows.delete(receipt.id);
      else rows.set(receipt.id, receipt.target);
      current = { revision: receipt.revision, publication: receipt.publication, publishedRevision: receipt.publishedRevision,
        targets: [...rows.values()], complete: false };
      return "applied";
    }
    return { current: () => current, read, save };
  }

  function target(row, settings) {
    if (row.kind === "dns") return dns.target(row);
    return { ...model.targetFromMetric(endpoint(row), settings), name: row.name, subtitle: row.group,
      customGroup: row.group };
  }

  function fixedDiscovered(catalog, discovery) {
    if (!catalog || catalog.complete === false || discovery?.revision !== catalog.revision) return false;
    const schemaTwo = catalog.manifestRevision === catalog.revision;
    if (!schemaTwo && (discovery.discoveredRevision !== catalog.revision ||
        discovery.publishedRevision !== catalog.revision || catalog.publishedRevision !== catalog.revision)) return false;
    const rows = discovery.activeTargets.filter((item) => !schemaTwo || item.resolutionGeneration == null);
    const active = new Map(rows.map((item) => [item.id, item]));
    const enabled = catalog.targets.filter((row) => row.enabled && row.kind !== "dns");
    return rows.length === enabled.length && enabled.every((row) =>
      active.get(row.id)?.ip === row.ip && active.get(row.id)?.revision === row.revision);
  }

  function catalogSync(catalog, discovery, notice, at) {
    if (!catalog) return "";
    if (catalog.publication === "pending") return "待发布";
    if (notice || catalog.publication !== "published" || catalog.publishedRevision !== catalog.revision ||
        discovery?.publishedRevision !== catalog.revision || discovery.discoveredRevision !== catalog.revision ||
        !fixedDiscovered(catalog, discovery)) return "待发现";

    // D2 acknowledges the projected set, which can omit an unresolved DNS
    // row. Overall sync also requires every enabled configuration to be found.
    const enabled = catalog.targets.filter((row) => row.enabled);
    return discovery.activeTargets.length === enabled.length &&
      !discovery.dnsTargets?.some((row) => row.withdrawalPending) &&
      enabled.every((row) => row.kind !== "dns" || dns.verifiedEndpoint(catalog, discovery, row, at))
      ? "已发现" : "待发现";
  }

  function present(catalog, discovery, samples, settings, observed = new Set(), notice = "", now = Date.now() / 1000) {
    if (!catalog) return [];
    const discovered = fixedDiscovered(catalog, discovery);
    return catalog.targets.filter((row) => row.enabled).map((row) => {
      if (row.kind === "dns") return dns.presentation(catalog, discovery, row, samples, settings, notice, now);
      const item = target(row, settings);
      const reading = samples?.get(item.key);
      const since = Date.parse(row.updatedAt) / 1000;
      let sync = catalog.publication === "pending" ? "待发布" : "待发现";
      let reason = notice;
      if (discovered) {
        sync = "已发现";
        const attempt = item.key + ":" + row.revision;
        if (reading?.sourceAt != null && reading.sourceAt >= since && reading.status !== "UNKNOWN" && !notice) {
          observed.add(attempt);
          const loss = reading.sourceAt - since >= item.lossWindow ? reading.loss : model.lossStats(null, null, 60, 2);
          const status = reading.status === "DOWN" ? "DOWN" :
            loss.loss >= settings.lossThreshold || reading.rtt >= settings.rttThreshold ? "DEGRADED" : "UP";
          return { ...item, sync, status, rtt: reading.rtt, loss, sourceAt: reading.sourceAt, reason: "" };
        }
        reason ||= reading?.reason || "";
        if (!notice && !observed.has(attempt) && !["Prometheus 抓取失败", "数据过期"].includes(reason)) {
          sync = "待首次探测";
        }
      }
      return { ...item, sync, reason, status: "UNKNOWN", rtt: null, sourceAt: null, loss: model.lossStats(null, null, 60, 2) };
    }).sort((a, b) => groups.indexOf(a.customGroup) - groups.indexOf(b.customGroup)
      || a.name.localeCompare(b.name, "zh-CN", { numeric: true }) || a.key.localeCompare(b.key));
  }

  function createMonitor({ client, sample, settings, changed, visible, now }) {
    const state = createState();
    const observed = new Set();
    let active = false;
    let controller;
    let timer;
    let expiryTimer;
    let failures = 0;
    let notice = "";
    let items = [];
    let renderedSync = "";

    function render(discovery, samples) {
      const catalog = state.current();
      const at = now();
      items = present(catalog, discovery, samples, settings, observed, notice, at);
      renderedSync = catalogSync(catalog, discovery, notice, at);
      scheduleExpiry();
      if (observed.size > 384) observed.clear();
      changed({ catalog, items, notice, sync: renderedSync });
    }

    // A status is only live while its sample is inside the same window the
    // Prometheus snapshot applies.
    const maxAge = (item) => Math.max(10, item.interval * 3);
    const stale = (item, at) => item.sourceAt !== null && item.status !== "UNKNOWN" &&
      (at - item.sourceAt > maxAge(item) || (item.dns && at >= item.expiresAt));
    const retired = (item) => ({ ...item, status: "UNKNOWN", rtt: null, sourceAt: null, reason: "数据过期",
      loss: model.lossStats(null, null, item.lossWindow, item.interval) });

    function scheduleExpiry() {
      clearTimeout(expiryTimer);
      const live = items.filter((item) => item.dns && item.dnsStatus === "DNS_OK" &&
        (item.status !== "UNKNOWN" || renderedSync === "已发现"));
      if (!active || !visible() || !live.length) return;
      const when = Math.min(...live.map((item) => item.status === "UNKNOWN" ? item.expiresAt :
        Math.min(item.expiresAt, item.sourceAt + 10.001)));
      expiryTimer = setTimeout(() => { expire(); scheduleExpiry(); }, Math.max(1, Math.ceil((when - now()) * 1000)));
    }

    // Cached statuses are checked whenever a read starts, so hidden tabs, SPA
    // route changes and stalled reads cannot keep a retired UP on screen.
    function expire(retireDns = false) {
      const at = now();
      let expired = false;
      items = items.map((item) => {
        if (item.dns && item.dnsStatus === "DNS_OK" && Number.isFinite(item.expiresAt) && at >= item.expiresAt) {
          expired = true;
          if (renderedSync === "已发现") renderedSync = "待发现";
          return { ...retired(item), dnsStatus: "DNS_STALE", dnsLabel: "DNS：解析已过期", sync: "等待重新解析" };
        }
        if (item.status === "UNKNOWN" || (retireDns ? !item.dns : !stale(item, at))) return item;
        expired = true;
        return item.dns ? { ...retired(item), sync: "等待新样本" } : retired(item);
      });
      if (!expired) return;
      // Keep the authoritative catalog, card identity and history links; only
      // the stale readings are retired until a new sample is accepted.
      changed({ catalog: state.current(), items, notice, sync: renderedSync });
    }

    // The snapshot validator accepts a sample relative to its query time, but a
    // delayed read hands it back after the window already closed. Retire it
    // before render() can emit any changed() event, so an expired sample is
    // never published as UP even transiently.
    function freshSamples(samples) {
      if (!samples) return samples;
      const at = now();
      const fresh = new Map();
      for (const [key, reading] of samples) fresh.set(key, reading && stale(reading, at) ? retired(reading) : reading);
      return fresh;
    }

    function cancelRequest() {
      clearTimeout(timer);
      controller?.abort();
      controller = null;
    }

    function pause() {
      cancelRequest();
      clearTimeout(expiryTimer);
      expire(true);
    }

    async function refresh() {
      cancelRequest();
      expire();
      if (!active || !visible()) return false;
      const request = new AbortController();
      controller = request;
      const { signal } = request;
      const at = now();
      let discovery;
      let samples;
      let authorityCurrent = false;
      try {
        const [list, found] = await Promise.allSettled([client.list(signal), client.discovery(signal)]);
        if (signal.aborted) return false;
        if (list.status !== "fulfilled") throw list.reason;
        if (!state.read(list.value)) throw new Error("旧的自定义清单");
        authorityCurrent = true;
        if (found.status !== "fulfilled" || !validDiscovery(found.value) || found.value.revision !== list.value.revision) {
          throw new Error("自定义发现暂不可确认");
        }
        discovery = found.value;
        const catalog = state.current();
        const dnsRows = catalog.targets.filter((row) => row.kind === "dns");
        const ready = dnsRows.map((row) => dns.verifiedEndpoint(catalog, discovery, row, now())).filter(Boolean);
        const byKey = new Map(ready.map((item) => [item.key, item]));
        let dnsChanged = false;
        items = items.map((item) => {
          if (!item.dns) return item;
          const current = byKey.get(item.key);
          if (current?.resolutionGeneration === item.resolutionGeneration && current?.ip === item.ip &&
              current?.sampleNotBefore === item.sampleNotBefore) return item;
          dnsChanged = true;
          const row = dnsRows.find(({ id }) => id === item.metric.custom_id);
          return row?.enabled ? dns.presentation(catalog, discovery, row, null, settings, "", now()) : null;
        }).filter(Boolean);
        const nextSync = catalogSync(catalog, discovery, notice, now());
        if (dnsChanged || renderedSync === "已发现" && nextSync !== "已发现") {
          renderedSync = nextSync;
          scheduleExpiry();
          changed({ catalog, items, notice, sync: renderedSync });
        }
        const fixed = catalog.targets.some((row) => row.enabled && row.kind !== "dns") && fixedDiscovered(catalog, discovery);
        if (fixed || ready.length) {
          samples = await sample(at, signal, ready, fixed);
          if (signal.aborted) return false;
          if (ready.length) {
            // The sample fetch may span a DNS generation change independent of
            // manifest revision. Re-read the authoritative row before painting.
            const latest = await client.list(signal);
            if (signal.aborted) return false;
            if (!state.read(latest)) throw new Error("旧的自定义清单");
          }
          samples = freshSamples(samples);
        }
        failures = 0;
        notice = "";
      } catch {
        if (signal.aborted) return false;
        failures = Math.min(failures + 1, 4);
        notice = authorityCurrent ? "自定义探测或发现暂不可用" : "自定义清单暂不可用";
      }
      render(discovery, samples);
      if (active && !signal.aborted && visible()) {
        timer = setTimeout(refresh, Math.min(60000, 5000 * 2 ** failures));
      }
      return authorityCurrent;
    }

    function saved(receipt) {
      const outcome = state.save(receipt);
      notice = outcome === "reread" ? "已确认保存，需重新读取权威清单" : "";
      render();
      void refresh();
    }

    return { current: state.current, refresh, saved, pause,
      start() { active = true; failures = 0; void refresh(); },
      stop() { active = false; pause(); } };
  }

  const api = { groups, target, present, createState, createMonitor };
  if (common) module.exports = api;
  else window.BSPingCustomModel = api;
})();
