;(function () {
  'use strict';

  function createDhcpPanel({
    document,
    window: browserWindow,
    model,
    escapeHtml,
    groupAddressesByCBlock,
    setText,
    fetchDhcpDashboard,
    fetchDhcpBindings,
    isPageActive,
    onDataSuccess
  }) {
    const {
      dhcpRangeAddresses, compactDhcpAddresses, dhcpPoolKey,
      dhcpPoolMatchesSearch, dhcpPoolMatchesFilter, compareDhcpPools,
      buildDhcpAddressContext, dhcpAddressState
    } = model;

    let dhcpTimer = null;
    let dhcpSeq = 0;
    let dhcpRefreshing = false;
    let dhcpHasData = false;
    let dhcpLastPayload = null;
    let dhcpBindingPayload = null;
    let dhcpBindingsRefreshing = false;
    let dhcpSelectedPoolKey = "";
    let dhcpPoolSearchText = "";
    let dhcpPoolFilterValue = "all";

    function stop() {
      if (dhcpTimer) {
        browserWindow.clearTimeout(dhcpTimer);
        dhcpTimer = null;
      }
      dhcpSeq += 1;
      dhcpRefreshing = false;
    }

    function dhcpSummaryCard(label, value, note, level = "") {
      return `
        <div class="dhcp-summary-card ${level}">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
          <small>${escapeHtml(note || "")}</small>
        </div>
      `;
    }

    function dhcpPoolCard(pool, conflicts) {
      const pct = Math.max(0, Math.min(100, Number(pool.utilization || 0)));
      const addressBlockCount = groupAddressesByCBlock(dhcpRangeAddresses(pool.range)).length;
      return `
        <article class="dhcp-pool-card ${escapeHtml(pool.level || "good")}${addressBlockCount > 1 ? " multi-block" : ""}">
          <header>
            <div><strong>${escapeHtml(pool.name || "未命名地址池")}</strong><span>${escapeHtml(pool.range || "交换机未返回地址范围")}</span></div>
            <b>${pct.toFixed(1)}%</b>
          </header>
          <div class="dhcp-pool-bar"><i style="width:${pct}%"></i></div>
          <dl>
            <div><dt>总地址</dt><dd>${Number(pool.total || 0)}</dd></div>
            <div><dt>已租用</dt><dd>${Number(pool.leased || 0)}</dd></div>
            <div><dt>剩余</dt><dd>${Number(pool.available || 0)}</dd></div>
            <div><dt>排除</dt><dd>${Number(pool.excluded || 0)}</dd></div>
          </dl>
          ${dhcpAddressMap(pool, conflicts, dhcpBindingPayload)}
        </article>
      `;
    }

    function renderDhcpPoolBrowser(pools, conflicts) {
      const poolsElement = document.getElementById("dhcpPools");
      if (!poolsElement) return;
      const previousDirectory = poolsElement.querySelector(".dhcp-pool-directory");
      const previousDetail = poolsElement.querySelector(".dhcp-pool-detail");
      const directoryScrollTop = previousDirectory ? previousDirectory.scrollTop : 0;
      const detailScrollTop = previousDetail ? previousDetail.scrollTop : 0;
      const previousSelectedPoolKey = dhcpSelectedPoolKey;
      const sortedPools = [...pools].sort(compareDhcpPools);
      const visiblePools = sortedPools.filter((pool) =>
        dhcpPoolMatchesSearch(pool, dhcpPoolSearchText) && dhcpPoolMatchesFilter(pool, conflicts, dhcpPoolFilterValue)
      );
      if (!visiblePools.some((pool) => dhcpPoolKey(pool) === dhcpSelectedPoolKey)) {
        dhcpSelectedPoolKey = visiblePools.length ? dhcpPoolKey(visiblePools[0]) : "";
      }
      const selectedPool = visiblePools.find((pool) => dhcpPoolKey(pool) === dhcpSelectedPoolKey);
      setText("dhcpPoolCount", `显示 ${visiblePools.length} / ${pools.length} 个网段`);
      if (!visiblePools.length) {
        poolsElement.innerHTML = `<div class="dhcp-empty">没有符合当前搜索或筛选条件的网段。</div>`;
        return;
      }
      poolsElement.innerHTML = `
        <aside class="dhcp-pool-directory" aria-label="DHCP 网段目录">
          ${visiblePools.map((pool) => {
            const key = dhcpPoolKey(pool);
            const pct = Math.max(0, Math.min(100, Number(pool.utilization || 0)));
            return `
              <button type="button" class="dhcp-pool-option ${escapeHtml(pool.level || "good")}${key === dhcpSelectedPoolKey ? " selected" : ""}" data-dhcp-pool="${escapeHtml(key)}">
                <span><strong>${escapeHtml(pool.name || "未命名地址池")}</strong><small>${escapeHtml(pool.range || "无地址范围")}</small></span>
                <span><b>${Number(pool.leased || 0)} 已用</b><small>${pct.toFixed(1)}%</small></span>
              </button>
            `;
          }).join("")}
        </aside>
        <div class="dhcp-pool-detail">
          ${selectedPool ? dhcpPoolCard(selectedPool, conflicts) : ""}
        </div>
      `;
      const selectionChanged = previousSelectedPoolKey !== dhcpSelectedPoolKey;
      const nextDirectory = poolsElement.querySelector(".dhcp-pool-directory");
      const nextDetail = poolsElement.querySelector(".dhcp-pool-detail");
      if (nextDirectory) nextDirectory.scrollTop = selectionChanged ? 0 : directoryScrollTop;
      if (nextDetail) nextDetail.scrollTop = selectionChanged ? 0 : detailScrollTop;
    }

    function dhcpAddressMap(pool, conflicts, bindingPayload) {
      const addresses = dhcpRangeAddresses(pool.range);
      if (!addresses.length) return '<div class="dhcp-address-note">交换机未返回可展开的地址范围。</div>';
      const addressBlocks = groupAddressesByCBlock(addresses);
      const {
        excluded, conflictSet, bindingDetails, arpDetails, used, reservedUsed
      } = buildDhcpAddressContext(pool, conflicts, bindingPayload);
      const excludedList = [...excluded];
      const exclusionNote = excludedList.length
        ? `排除地址：${compactDhcpAddresses(excludedList)}`
        : (Number(pool.excluded || 0) ? "交换机返回了排除数量，但未返回具体排除配置" : "没有排除地址");
      return `
        <section class="dhcp-address-map" aria-label="${escapeHtml(pool.name || "地址池")} IP 地址格">
          <div class="dhcp-address-map-head">
            <div class="dhcp-address-legend">
              <span><i class="pool"></i>池内地址</span>
              <span><i class="used"></i>已用</span>
              <span><i class="excluded"></i>排除（未发现）</span>
              <span><i class="reserved-used"></i>排除且已发现</span>
              <span><i class="conflict"></i>冲突</span>
            </div>
            <span class="dhcp-exclusion-list">${escapeHtml(exclusionNote)}</span>
          </div>
          <div class="dhcp-address-blocks">
            ${addressBlocks.map((block) => `
              <div class="dhcp-address-block">
                <strong>${escapeHtml(`${block.prefix}.0/24`)}</strong>
                <div class="dhcp-address-grid">
                  ${block.addresses.map((ip) => {
                    const status = dhcpAddressState(ip, conflictSet, reservedUsed, excluded, used);
                    const label = ip.slice(ip.lastIndexOf("."));
                    const statusText = status === "conflict" ? "冲突"
                      : status === "reserved-used"
                        ? `排除地址已发现${arpDetails.get(ip) ? ` · ${arpDetails.get(ip)}` : bindingDetails.get(ip) ? ` · ${bindingDetails.get(ip)}` : ""}`
                      : status === "excluded" ? "排除地址（当前租约和 ARP 表均未发现）" : status === "used"
                      ? `已租用${bindingDetails.get(ip) ? ` · ${bindingDetails.get(ip)}` : ""}`
                      : (bindingPayload ? "未在当前租约表中" : "池内（点击“查询已用 IP”后标色）");
                    return `<span class="dhcp-address-cell ${status}" title="${escapeHtml(`${ip} · ${statusText}`)}" aria-label="${escapeHtml(`${ip} ${statusText}`)}">${escapeHtml(label)}</span>`;
                  }).join("")}
                </div>
              </div>
            `).join("")}
          </div>
        </section>
      `;
    }

    function renderDhcpDashboard(payload) {
      dhcpLastPayload = payload;
      const summary = payload.summary || {};
      const pools = payload.pools || [];
      const conflicts = payload.conflicts || [];
      const captured = payload.capturedAt
        ? new Date(payload.capturedAt * 1000).toLocaleTimeString("zh-CN", { hour12: false })
        : "—";
      const refreshSeconds = Number(payload.refreshSeconds || 60);
      setText("dhcpConnection", `${payload.host || "—"} · 读取自基础配置 · ${refreshSeconds} 秒刷新`);

      const status = document.getElementById("dhcpStatus");
      if (status) {
        status.className = "dhcp-status good";
        status.textContent = payload.refreshing
          ? `正在刷新，当前显示上次结果 · 采集于 ${captured}`
          : `${payload.cached ? `使用 ${Number(payload.cacheAgeSeconds || 0).toFixed(0)} 秒内缓存` : `已从核心交换机刷新（${Number(payload.collectionSeconds || 0).toFixed(2)} 秒）`} · 采集于 ${captured}`;
      }

      const utilization = Number(summary.utilization || 0);
      const utilizationLevel = utilization >= 90 ? "bad" : utilization >= 80 ? "warn" : "good";
      const summaryElement = document.getElementById("dhcpSummary");
      if (summaryElement) {
        summaryElement.innerHTML = [
          dhcpSummaryCard("地址池", String(summary.poolCount || 0), "核心交换机"),
          dhcpSummaryCard("可分配地址", String(summary.total || 0), `排除 ${summary.excluded || 0}`),
          dhcpSummaryCard("已租用", String(summary.leased || 0), "当前活动地址"),
          dhcpSummaryCard("剩余", String(summary.available || 0), "仍可分配"),
          dhcpSummaryCard("总体使用率", `${utilization.toFixed(1)}%`, "80% 提醒 / 90% 告警", utilizationLevel),
          dhcpSummaryCard("冲突地址", String(summary.conflictCount || 0), conflicts.slice(0, 3).join("、") || "未发现冲突", conflicts.length ? "bad" : "good")
        ].join("");
      }

      if (pools.length) renderDhcpPoolBrowser(pools, conflicts);
      else {
        const poolsElement = document.getElementById("dhcpPools");
        if (poolsElement) poolsElement.innerHTML = `<div class="dhcp-empty">核心交换机当前没有返回 DHCP 地址池。</div>`;
        setText("dhcpPoolCount", "0 个网段");
      }

      const warningText = (payload.warnings || []).join("；");
      setText(
        "dhcpFootnote",
        `${warningText ? `${warningText} · ` : ""}地址池数量自动刷新；进入页面时读取租约与 ARP 表。排除地址未被发现不代表设备一定离线（设备可能不响应或 ARP 已老化）。`
      );
      if (!dhcpBindingPayload && !dhcpBindingsRefreshing) {
        browserWindow.setTimeout(refreshDhcpBindings, 0);
      }
    }

    async function refreshDhcpBindings() {
      if (!isPageActive() || dhcpBindingsRefreshing) return;
      const button = document.getElementById("dhcpBindings");
      const status = document.getElementById("dhcpBindingsStatus");
      dhcpBindingsRefreshing = true;
      if (button) button.disabled = true;
      if (status) status.textContent = "正在读取租约与 ARP 表…";
      try {
        const payload = await fetchDhcpBindings();
        if (!isPageActive()) return;
        dhcpBindingPayload = payload;
        if (status) {
          const captured = payload.capturedAt
            ? new Date(payload.capturedAt * 1000).toLocaleTimeString("zh-CN", { hour12: false })
            : "刚刚";
          const returned = Number((payload.usedAddresses || []).length);
          const exclusions = new Set((dhcpLastPayload && dhcpLastPayload.pools || [])
            .flatMap((pool) => pool.excludedAddresses || []));
          const discovered = new Set([
            ...(payload.usedAddresses || []),
            ...(payload.observedAddresses || [])
          ]);
          const reservedUsed = [...exclusions].filter((ip) => discovered.has(ip)).length;
          const expected = (dhcpLastPayload && dhcpLastPayload.pools || [])
            .reduce((sum, pool) => sum + Number(pool.leased || 0), 0);
          const statusText = returned === 0 && expected > 0
            ? `交换机统计已租用 ${expected} 个，但租约明细未解析；${payload.parserWarning || "请重试或检查命令输出"}`
            : `DHCP 租约（绿色）${returned} 个 · 排除且已发现（蓝色）${reservedUsed} 个 · ${captured}`;
          status.textContent = payload.arpWarning ? `${statusText} · ${payload.arpWarning}` : statusText;
        }
        if (dhcpLastPayload) renderDhcpDashboard(dhcpLastPayload);
      } catch (error) {
        if (status) status.textContent = `已用 IP 查询失败：${error.message || "未知错误"}`;
      } finally {
        dhcpBindingsRefreshing = false;
        if (button) button.disabled = false;
      }
    }

    function scheduleDhcpRefresh(seconds = 60) {
      if (dhcpTimer) browserWindow.clearTimeout(dhcpTimer);
      if (!isPageActive() || document.visibilityState === "hidden") {
        dhcpTimer = null;
        return;
      }
      dhcpTimer = browserWindow.setTimeout(() => refreshDhcpDashboard(false), Math.max(30, Number(seconds || 60)) * 1000);
    }

    async function refreshDhcpDashboard(force = false) {
      if (!isPageActive() || document.visibilityState === "hidden" || dhcpRefreshing) return;
      const seq = ++dhcpSeq;
      dhcpRefreshing = true;
      const refreshButton = document.getElementById("dhcpRefresh");
      if (refreshButton) refreshButton.disabled = true;
      const status = document.getElementById("dhcpStatus");
      if (status) {
        status.className = "dhcp-status loading";
        status.textContent = dhcpHasData ? "正在从核心交换机刷新…" : "正在连接核心交换机并读取 DHCP…";
      }
      let nextSeconds = 60;
      try {
        const payload = await fetchDhcpDashboard(force);
        if (seq !== dhcpSeq || !isPageActive()) return;
        dhcpHasData = true;
        nextSeconds = Number(payload.refreshSeconds || 60);
        renderDhcpDashboard(payload);
        onDataSuccess();
      } catch (error) {
        if (seq !== dhcpSeq || !isPageActive()) return;
        if (status) {
          status.className = "dhcp-status bad";
          status.textContent = `读取失败：${error.message || "未知错误"}`;
        }
        if (!dhcpHasData) {
          const poolsElement = document.getElementById("dhcpPools");
          const summaryElement = document.getElementById("dhcpSummary");
          if (summaryElement) summaryElement.innerHTML = "";
          if (poolsElement) poolsElement.innerHTML = `
            <div class="dhcp-empty bad">
              <span>请检查核心 IP、Telnet 登录信息和交换机连通性。</span>
              <a class="dhcp-config-link" href="/control#core-telnet">去赛事控制台配置</a>
            </div>
          `;
        }
      } finally {
        if (seq === dhcpSeq) {
          dhcpRefreshing = false;
          if (refreshButton) refreshButton.disabled = false;
          scheduleDhcpRefresh(nextSeconds);
        }
      }
    }

    function start() {
      stop();
      const refreshButton = document.getElementById("dhcpRefresh");
      if (refreshButton && !refreshButton.dataset.bound) {
        refreshButton.addEventListener("click", () => refreshDhcpDashboard(true));
        refreshButton.dataset.bound = "1";
      }
      const bindingsButton = document.getElementById("dhcpBindings");
      if (bindingsButton && !bindingsButton.dataset.bound) {
        bindingsButton.addEventListener("click", refreshDhcpBindings);
        bindingsButton.dataset.bound = "1";
      }
      const poolSearch = document.getElementById("dhcpPoolSearch");
      if (poolSearch && !poolSearch.dataset.bound) {
        poolSearch.addEventListener("input", () => {
          dhcpPoolSearchText = poolSearch.value;
          if (dhcpLastPayload) renderDhcpPoolBrowser(dhcpLastPayload.pools || [], dhcpLastPayload.conflicts || []);
        });
        poolSearch.dataset.bound = "1";
      }
      const poolFilter = document.getElementById("dhcpPoolFilter");
      if (poolFilter && !poolFilter.dataset.bound) {
        poolFilter.addEventListener("change", () => {
          dhcpPoolFilterValue = poolFilter.value;
          if (dhcpLastPayload) renderDhcpPoolBrowser(dhcpLastPayload.pools || [], dhcpLastPayload.conflicts || []);
        });
        poolFilter.dataset.bound = "1";
      }
      const poolsElement = document.getElementById("dhcpPools");
      if (poolsElement && !poolsElement.dataset.bound) {
        poolsElement.addEventListener("click", (event) => {
          const option = event.target.closest("[data-dhcp-pool]");
          if (!option) return;
          dhcpSelectedPoolKey = option.dataset.dhcpPool || "";
          if (dhcpLastPayload) {
            renderDhcpPoolBrowser(dhcpLastPayload.pools || [], dhcpLastPayload.conflicts || []);
            const detail = poolsElement.querySelector(".dhcp-pool-detail");
            if (detail) detail.scrollTop = 0;
          }
        });
        poolsElement.dataset.bound = "1";
      }
      refreshDhcpDashboard(false);
    }

    function hasScheduledRefresh() {
      return Boolean(dhcpTimer);
    }

    document.addEventListener("visibilitychange", () => {
      if (!isPageActive()) return;
      if (document.visibilityState === "hidden") stop();
      else start();
    });

    return { start, stop, hasScheduledRefresh };
  }

  const ns = { createDhcpPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSDhcpPanel = ns;
  }
}());
