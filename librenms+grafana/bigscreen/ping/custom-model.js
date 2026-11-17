;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const groups = Object.freeze(["Remote", "Server", "Other"]);
  const revision = (value) => Number.isSafeInteger(value) && value >= 0;
  const publication = (value) => revision(value.revision) &&
    (value.publication === "published" && value.publishedRevision === value.revision ||
     value.publication === "pending" && value.publishedRevision === null);
  const endpoint = ({ id, ip }) => ({ job: "custom-ping", custom_id: id, instance: id, target_ip: ip });
  const validEndpoint = (row) => !!row && model.customMetric(endpoint(row)) && revision(row.revision) && row.revision > 0;

  function validTarget(row) {
    return validEndpoint(row) && typeof row.name === "string" && row.name.length > 0 && groups.includes(row.group) &&
      typeof row.enabled === "boolean" && Number.isFinite(Date.parse(row.updatedAt));
  }

  function validDiscovery(value) {
    return value?.ok === true && revision(value.revision) &&
      (value.publishedRevision === null || value.publishedRevision === value.revision) &&
      (value.discoveredRevision === null || value.discoveredRevision === value.revision) &&
      Array.isArray(value.activeTargets) && value.activeTargets.length <= 100 && value.activeTargets.every(validEndpoint);
  }

  function createState() {
    let current = null;
    let floor = 0;

    function read(value) {
      if (value?.ok !== true || !publication(value) || !Array.isArray(value.targets) || value.targets.length > 300 ||
          !value.targets.every((row) => validTarget(row) && row.revision <= value.revision) ||
          new Set(value.targets.map(({ id }) => id)).size !== value.targets.length) {
        throw new Error("自定义清单响应无效");
      }
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
    return { ...model.targetFromMetric(endpoint(row), settings), name: row.name, subtitle: row.group,
      customGroup: row.group };
  }

  function isDiscovered(catalog, discovery) {
    if (!catalog || catalog.complete === false || discovery?.revision !== catalog.revision ||
        discovery.discoveredRevision !== catalog.revision || discovery.publishedRevision !== catalog.revision ||
        catalog.publishedRevision !== catalog.revision) return false;
    const active = new Map(discovery.activeTargets.map((item) => [item.id, item]));
    const enabled = catalog.targets.filter((row) => row.enabled);
    return discovery.activeTargets.length === enabled.length && enabled.every((row) =>
      active.get(row.id)?.ip === row.ip && active.get(row.id)?.revision === row.revision);
  }

  function present(catalog, discovery, samples, settings, observed = new Set(), notice = "") {
    if (!catalog) return [];
    const discovered = isDiscovered(catalog, discovery);
    return catalog.targets.filter((row) => row.enabled).map((row) => {
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
    let failures = 0;
    let notice = "";
    let items = [];
    let renderedSync = "";

    function render(discovery, samples) {
      const catalog = state.current();
      items = present(catalog, discovery, samples, settings, observed, notice);
      if (observed.size > 384) observed.clear();
      renderedSync = !catalog ? "" : catalog.publication === "pending" ? "待发布" :
        !notice && isDiscovered(catalog, discovery) ? "已发现" : "待发现";
      changed({ catalog, items, notice, sync: renderedSync });
    }

    // A cached status is only live while its sample is inside the same window
    // the Prometheus snapshot applies. Hidden tabs, SPA route changes and
    // stalled reads otherwise keep a retired UP on screen until the next read.
    function expire() {
      const at = now();
      let expired = false;
      items = items.map((item) => {
        if (item.status === "UNKNOWN" || item.sourceAt === null ||
            at - item.sourceAt <= Math.max(10, item.interval * 3)) return item;
        expired = true;
        return { ...item, status: "UNKNOWN", rtt: null, sourceAt: null, reason: "数据过期",
          loss: model.lossStats(null, null, item.lossWindow, item.interval) };
      });
      if (!expired) return;
      // Keep the authoritative catalog, card identity and history links; only
      // the stale readings are retired until a new sample is accepted.
      changed({ catalog: state.current(), items, notice, sync: renderedSync });
    }

    function pause() {
      clearTimeout(timer);
      controller?.abort();
      controller = null;
    }

    async function refresh() {
      pause();
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
        if (discovery.activeTargets.length && isDiscovered(state.current(), discovery)) {
          samples = await sample(at, signal);
          if (signal.aborted) return false;
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
        // A slow authority read may have used samples that were fresh when the
        // read started but are already retired by the time they are painted.
        expire();
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
