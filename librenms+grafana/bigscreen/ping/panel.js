;(function () {
  'use strict';

  const model = window.BSPingModel;
  const charts = window.BSPingCharts;
  const history = window.BSPingHistory;
  const { createHistoryPanel, localInput } = window.BSPingDetail;
  const { createTrendLoader, refreshSeconds } = window.BSPingTrends;
  const { queryError } = window.BSPingData;
  const states = { UP: "在线", DOWN: "离线", DEGRADED: "质量异常", UNKNOWN: "未知" };
  const now = () => Math.floor(Date.now() / 1000);

  function createPingPanel(root, data, settings) {
    const firewall = window.BSOverviewModel.firewallExpectations(window.BIGSCREEN_CONFIG?.firewallPing);
    const byId = (id) => root.querySelector(`#ping${id}`);
    const category = byId("Category");
    const status = byId("Status");
    const search = byId("Search");
    const wallView = byId("WallView");
    const detail = byId("Detail");
    const requests = new Map();
    const selected = new Map();
    let active = false;
    let inHistory = false;
    let timer = null;
    let targets = [];
    let baseTargets = [];
    let custom = { catalog: null, items: [], notice: "" };
    let agedOut = 0;
    let visible = new Map();
    let nearby = [];
    let lastSuccess = null;
    let dataState = "loading";
    let dataError = "";
    let snapshotAt = null;
    let restore = null;
    let restoreFrame = null;
    const wall = window.BSPingWall.createPingWall({ root, grid: byId("Wall"), viewportChanged });
    const trends = createTrendLoader({ raw: data.raw, changed: wall.paint });
    const historyPanel = createHistoryPanel({ byId, data, run, cancel, replaceUrl, back: backToWall });
    const client = window.BSPingCustomAPI.createClient({ fetch: window.fetch.bind(window), crypto: window.crypto });
    const customMonitor = window.BSPingCustomModel.createMonitor({ client, sample: data.custom, settings, now,
      visible: () => !document.hidden, changed: (update) => {
        custom = update;
        if (active && !inHistory && snapshotAt === null) renderSnapshot();
      } });
    const customManager = window.BSPingCustomManager.createManager({ byId, client, monitor: customMonitor, auth: window.BSApi });

    function cancel(name) {
      requests.get(name)?.abort();
      requests.delete(name);
    }

    async function run(name, load, done, failed) {
      cancel(name);
      const controller = new AbortController();
      requests.set(name, controller);
      try {
        const result = await load(controller.signal);
        if (!active || requests.get(name) !== controller) return false;
        done(result);
        return true;
      } catch (error) {
        if (!active || requests.get(name) !== controller) return false;
        failed(error);
        return true;
      } finally {
        if (requests.get(name) === controller) requests.delete(name);
      }
    }

    function list() {
      const term = search.value.trim().toLowerCase();
      const filtered = targets.filter((target) =>
        (!category.value || category.value === target.category || category.value === target.group ||
          category.value === `custom-${target.customGroup}`) &&
        (!status.value || (status.value === "abnormal" ? target.status !== "UP" : status.value === target.status)) &&
        (!term || [target.name, target.ip, target.subtitle, target.group, model.groupLabel(target)].some((value) => value.toLowerCase().includes(term))));
      visible = new Map(filtered.map((target) => [target.key, target]));
      wall.render(visible, selected);
      const empty = dataState === "loading" ? "正在读取监控目标…" :
        dataState === "unavailable" ? "Monitoring Unavailable · 监控数据不可用" : "没有符合条件的目标";
      byId("Empty").textContent = visible.size ? "" : empty;
      byId("VisibleCount").textContent = `显示 ${visible.size} / ${targets.length} 个目标`;
      byId("Comparison").disabled = !selected.size;
      byId("Comparison").textContent = `查看历史 / 对比（${selected.size}/${model.maxCompared}）`;
      refreshTrends();
    }

    function renderSnapshot() {
      const live = snapshotAt === null;
      const customItems = live ? custom.items : [];
      targets = [...baseTargets, ...customItems];
      if (live) {
        const byKey = new Map(customItems.map((target) => [target.key, target]));
        for (const [key, target] of selected) {
          if (target.category !== "custom") continue;
          if (byKey.has(key)) selected.set(key, byKey.get(key));
          else if (custom.catalog) selected.delete(key);
        }
      }
      byId("CustomActions").hidden = !live;
      byId("CustomNotice").textContent = live ? custom.notice : "";
      customManager.update(custom);
      const counts = { UP: 0, DOWN: 0, DEGRADED: 0, UNKNOWN: 0 };
      for (const { status } of targets) counts[status] += 1;
      const available = targets.length > counts.UNKNOWN;
      const missing = dataState === "ready" ? model.missingFirewall(targets, firewall.ips) : [];
      const partial = counts.UNKNOWN > 0 || agedOut > 0 || missing.length > 0 || firewall.invalid ||
        dataState === "unavailable" || (live && !!custom.notice);
      const mode = snapshotAt === null ? "实时监控" : "历史快照 · " + localInput(snapshotAt).replace("T", " ");
      const source = byId("Source");
      source.dataset.state = !available ? "UNKNOWN" : partial ? "DEGRADED" : "UP";
      source.textContent = dataState === "loading" ? "正在读取" :
        !available ? ["Monitoring Unavailable", dataError].filter(Boolean).join(" · ") : partial ? `${mode} · 部分不可用` : mode;
      byId("Counts").textContent = Object.entries(states).map(([key, label]) => `${label} ${counts[key]}`).join(" · ");
      const coverage = byId("Coverage");
      const coverageText = [
        firewall.invalid ? "FW VIP 配置无法完整核对" : "",
        missing.length ? `已配置 FW VIP 缺少 ${missing.length} 个监控目标` : "",
        agedOut ? `${agedOut} 个目标已超出 ${model.inventorySeconds / 60} 分钟观察窗口并移出列表，消失原因未知。` : ""
      ].filter(Boolean).join(" · ");
      if (coverage.textContent !== coverageText) coverage.textContent = coverageText;
      byId("Updated").textContent = `最后成功更新 ${lastSuccess === null ? "—" : charts.formatTime(lastSuccess)}`;
      list();
      restoreWall();
    }

    function viewportChanged(targets) {
      nearby = targets;
      refreshTrends();
    }

    function refreshTrends() {
      if (!active || inHistory) return;
      if (dataState !== "ready" && !custom.items.length) {
        trends.update([], null);
        return;
      }
      const end = snapshotAt ?? Math.floor(now() / refreshSeconds) * refreshSeconds;
      const current = nearby.map(({ key }) => visible.get(key)).filter(Boolean);
      trends.update(current, end);
      for (const { key } of current) wall.paint(key, trends.get(key));
    }

    function unavailable(reason) {
      dataState = "unavailable";
      dataError = reason;
      trends.stop();
      baseTargets = baseTargets.map((target) => ({ ...target, status: "UNKNOWN", rtt: null, sourceAt: null,
        loss: { ...target.loss, total: 0, success: 0, failed: 0, coverage: 0, loss: null }, reason }));
      renderSnapshot();
    }

    async function refreshSnapshot() {
      clearTimeout(timer);
      const at = snapshotAt ?? now();
      const applied = await run("summary", (signal) => data.summary(at, signal), (result) => {
        agedOut = result.agedOut;
        baseTargets = result.targets.sort(model.wallOrder);
        dataState = baseTargets.length ? "ready" : "unavailable";
        dataError = "";
        if (baseTargets.some((target) => target.sourceAt !== null)) lastSuccess = now();
        renderSnapshot();
      }, (error) => unavailable(`监控${queryError(error)}`));
      if (applied && active && !requests.has("summary") && !inHistory && snapshotAt === null) {
        clearTimeout(timer);
        timer = setTimeout(refreshSnapshot, Math.max(500, 5000 - (now() - at) * 1000));
      }
    }

    function setSnapshot(at) {
      snapshotAt = at;
      trends.stop();
      nearby = [];
      selected.clear();
      dataState = "loading";
      dataError = "";
      baseTargets = [];
      agedOut = 0;
      lastSuccess = null;
      renderSnapshot();
      refreshSnapshot();
    }

    function navigate(url, state = null) {
      window.history.pushState(state, "", url);
      window.dispatchEvent(new PopStateEvent("popstate", { state }));
    }

    function replaceUrl(url) {
      window.history.replaceState(window.history.state, "", url);
    }

    function showDetails(chosen, origin) {
      if (!chosen.length) return;
      const wallState = {
        top: root.scrollTop, category: category.value, status: status.value, search: search.value,
        selected: [...selected.keys()], focusKey: origin.closest(".ping-card")?.dataset.key ?? null
      };
      window.history.replaceState({ ...window.history.state, pingWall: wallState }, "");
      const end = snapshotAt ?? now();
      navigate(history.urlFor(chosen, { start: end - 300, end }, snapshotAt), { pingFromWall: true });
    }

    function restoreWall() {
      if (!restore || !active || inHistory || dataState === "loading") return;
      const state = restore;
      restore = null;
      restoreFrame = requestAnimationFrame(() => {
        restoreFrame = null;
        if (!active || inHistory) return;
        root.scrollTop = Number.isFinite(state.top) ? Math.max(0, state.top) : 0;
        const card = state.focusKey && [...byId("Wall").querySelectorAll(".ping-card")]
          .find((node) => node.dataset.key === state.focusKey);
        const focus = state.focusKey ? card?.querySelector(".ping-card-open") : byId("Comparison");
        (focus && !focus.disabled ? focus : search).focus({ preventScroll: true });
      });
    }

    function backToWall() {
      if (window.history.state?.pingFromWall) window.history.back();
      else navigate("/ping");
    }

    wallView.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-action]");
      const key = button?.closest(".ping-card")?.dataset.key;
      const target = visible.get(key);
      if (!target) return;
      if (button.dataset.action === "open") showDetails([target], button);
      if (button.dataset.action === "compare") {
        if (selected.has(key)) selected.delete(key);
        else if (selected.size < model.maxCompared) selected.set(key, target);
        list();
      }
    });
    for (const field of [category, status, search]) field.addEventListener("input", list);
    byId("Comparison").addEventListener("click", (event) => showDetails([...selected.values()], event.currentTarget));
    byId("Refresh").addEventListener("click", () => {
      void refreshSnapshot();
      if (snapshotAt === null) void customMonitor.refresh();
    });
    byId("SnapshotApply").addEventListener("click", () => {
      const at = Date.parse(byId("SnapshotAt").value) / 1000;
      if (!Number.isInteger(at) || at <= 300 || at > now()) {
        byId("Error").textContent = "请选择有效的历史快照时间。";
        return;
      }
      byId("Error").textContent = "";
      navigate("/ping?at=" + at);
    });
    byId("SnapshotLive").addEventListener("click", () => {
      byId("Error").textContent = "";
      if (snapshotAt !== null) navigate("/ping");
    });
    byId("Policy").textContent = `内置目标依据最近 ${model.inventorySeconds / 60} 分钟的抓取记录，超出窗口后移出列表。自定义目标来自已保存的启用清单。质量异常阈值为 Loss ≥ ${settings.lossThreshold}% 或当前 RTT ≥ ${settings.rttThreshold} ms。统计覆盖至少 80%，P50 / P95 / STDDEV 至少 30 个有效 RTT。`;

    function stop() {
      active = false;
      clearTimeout(timer);
      if (restoreFrame !== null) cancelAnimationFrame(restoreFrame);
      restoreFrame = null;
      wall.stop();
      trends.stop();
      historyPanel.stop();
      customMonitor.stop();
      customManager.stop();
      for (const name of requests.keys()) cancel(name);
    }

    function start(params) {
      stop();
      active = true;
      const route = history.parseRoute(params, now(), settings);
      inHistory = history.isHistory(params);
      wallView.hidden = inHistory;
      detail.hidden = !inHistory;
      root.setAttribute("aria-labelledby", inHistory ? "pingHistoryTitle" : "pingWorkspaceTitle");
      if (inHistory) {
        root.scrollTop = 0;
        const known = snapshotAt === route.at ? targets : [];
        historyPanel.start(route, known);
        if (route.targets.length && route.targets.some(({ key }) => !known.some((target) => target.key === key))) {
          run("summary", (signal) => data.summary(route.at ?? now(), signal),
            (result) => historyPanel.snapshot(result.targets), () => {});
        }
        return;
      }
      restore = window.history.state?.pingWall ?? null;
      if (restore) {
        category.value = restore.category;
        status.value = restore.status;
        search.value = restore.search;
      }
      wall.start();
      if (route.at === null) customMonitor.start();
      byId("SnapshotAt").value = localInput(route.at ?? now());
      if (snapshotAt !== route.at || !targets.length) setSnapshot(route.at);
      else refreshSnapshot();
      if (restore && Array.isArray(restore.selected)) {
        selected.clear();
        for (const key of restore.selected.slice(0, model.maxCompared)) {
          const target = history.parseTarget(key, settings);
          if (target) selected.set(key, target);
        }
        list();
      }
      root.scrollTop = restore?.top ?? 0;
      restoreWall();
    }

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) customMonitor.pause();
      else if (active && !inHistory && snapshotAt === null) void customMonitor.refresh();
    });
    window.addEventListener("pagehide", stop);
    window.addEventListener("pageshow", (event) => {
      if (event.persisted && /^\/ping\/?$/.test(window.location.pathname)) start(new URLSearchParams(window.location.search));
    });
    return { start, stop };
  }

  window.BSPingPanel = { createPingPanel };
})();
