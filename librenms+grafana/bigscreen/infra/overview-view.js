;(function () {
  'use strict';

  const { escapeHtml: esc } = window.BSUtils;
  const { pingHref } = window.BSOverviewModel;
  const business = window.BSBusinessTraffic;
  const maxFaults = 8;
  const healthLabels = { healthy: "All Good", fault: "检测到网络异常", partial: "监控部分不可用", unavailable: "监控不可用" };
  const faultLabels = { DOWN: "已确认离线", DEGRADED: "质量异常", UNKNOWN: "探测未知" };
  const formatTime = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const number = (value, places = 2) => value == null ? "N/A" : value.toFixed(places);
  const count = (values) => values.UP + values.DOWN + values.DEGRADED + values.UNKNOWN;

  function targetLink(target) {
    return `<a href="${pingHref(target)}" title="${esc(target.ip)}">${esc(target.name)}</a>`;
  }

  function badge(status) {
    return `<span class="overview-badge" data-status="${status}">${status}</span>`;
  }

  function sparkline(card) {
    const series = [card.in.points, card.out.points];
    const points = series.flat();
    const peak = points.reduce((max, point) => Math.max(max, point.v ?? 0), 0);
    if (!points.some(({ v }) => v !== null)) return '<div class="overview-no-trend">近 15 分钟暂无数据</div>';
    const start = points[0].t;
    const end = points.at(-1).t;
    const path = (values) => {
      let connected = false;
      return values.map(({ t, v }) => {
        if (v === null) {
          connected = false;
          return "";
        }
        const command = connected ? "L" : "M";
        connected = true;
        return `${command}${((t - start) * 198 / (end - start) + 1).toFixed(1)} ${(23 - v * 20 / (peak || 1)).toFixed(1)}`;
      }).join("");
    };
    return `<svg viewBox="0 0 200 26" preserveAspectRatio="none" role="img" aria-label="最近 15 分钟 WAN 入站和出站趋势">
      <path d="M0 23H200" class="overview-baseline" />
      <path d="${path(series[0])}" class="overview-rx" /><path d="${path(series[1])}" class="overview-tx" />
    </svg>`;
  }

  function direction(data, label, color) {
    const valid = data.bps !== null;
    const mbps = valid ? data.bps / 1e6 : null;
    const percent = valid && data.capacity ? mbps * 100 / data.capacity : null;
    const small = valid && data.bps > 0 && mbps < 0.1;
    const rate = small ? number(data.bps / 1000, 1) : number(mbps, 1);
    const rateText = small && rate === "0.0" ? "<0.1" : rate;
    return `<div class="overview-rate" data-direction="${color}" title="${esc(data.reason)}">
      <span>${label}</span><strong>${esc(rateText)}${valid ? ` <small>${small ? "Kbps" : "Mbps"}</small>` : ""}</strong>
      <div class="overview-capacity">${percent === null ? "" : `<div class="overview-util" aria-label="占配置容量 ${number(percent, 1)}%">
        <i style="width:${Math.min(100, percent)}%"></i></div>
        <small title="配置容量 ${data.capacity} Mbps">${number(percent, 1)}%<span class="overview-capacity-limit"> / ${data.capacity} Mbps</span></small>`}</div>
    </div>`;
  }

  function bandwidthCard(card) {
    const warnings = [...new Set([card.in.reason, card.out.reason].filter(Boolean))];
    const identity = card.instance && card.index ? `${card.instance} · ifIndex ${card.index}` : "接口待核实";
    const capacity = card.in.capacity !== null || card.out.capacity !== null ? "" :
      card.ambiguousName ? "同名接口 · 容量待核实" : "容量待核实";
    const probe = card.probeCoverage;
    const probeLabels = { missing: "网关未匹配探测", unconfigured: "未提供网关 · 探测关联待核实", ambiguous: "探测关联有歧义" };
    const probeText = probe.state === "matched"
      ? `<a href="${pingHref(probe.target)}">探测已关联 · ${probe.target.status}</a>`
      : probeLabels[probe.state];
    return `<article class="overview-wan" data-wan-key="${esc(card.key || "")}">
      <h4 title="${esc(`${card.name} · ${identity}`)}">${esc(card.name)}</h4>
      <div class="overview-rates">${direction(card.in, "入站 ↓", "in")}${direction(card.out, "出站 ↑", "out")}</div>
      ${sparkline(card)}
      <div class="overview-wan-notes">
        ${warnings.map((warning) => `<p class="overview-wan-note overview-warning">${esc(warning)}</p>`).join("")}
        ${capacity ? `<p class="overview-wan-note">${capacity}</p>` : ""}
        <p class="overview-wan-note${probe.state !== "matched" ? " overview-warning" : ""}" data-probe-coverage="${probe.state}">${probeText}</p>
      </div>
    </article>`;
  }

  function faultsMarkup(state) {
    const down = state.faults.filter(({ status }) => status === "DOWN");
    const rows = down.slice(0, maxFaults).map((target) => `<li>
      <div>${targetLink(target)} <small>${esc(target.group)}</small></div>${badge(target.status)}
    </li>`).join("");
    const other = ["DEGRADED", "UNKNOWN"].map((status) => {
      const list = state.faults.filter((target) => target.status === status);
      if (!list.length) return "";
      return `<section class="overview-other-faults"><b>${faultLabels[status]} ${list.length}</b>
        <span>${list.slice(0, 3).map(targetLink).join(" · ")}${list.length > 3 ? ' · <a href="/ping">其余目标</a>' : ""}</span></section>`;
    }).join("");
    return `${rows ? `<ul class="overview-faults">${rows}</ul>` : '<p class="overview-empty">暂无已确认离线设备</p>'}
      ${down.length > maxFaults ? '<p class="overview-note"><a href="/ping">查看其余离线设备</a></p>' : ""}${other}`;
  }

  function gatewayMarkup(state) {
    if (!state.gatewayRows.length) return '<p class="overview-empty">暂无可用的网关探测目标。</p>';
    const groups = [["内网 · 防火墙 / 核心", false], ["外网 · ISP", true]].map(([label, external]) => {
      const rows = state.gatewayRows.filter(({ category }) => (category === "isp") === external);
      if (!rows.length) return "";
      return `<tbody><tr class="overview-gateway-group"><th scope="rowgroup" colspan="5">${label}</th></tr>
        ${rows.map((target) => `<tr data-gateway>
          <th scope="row">${targetLink(target)} <small>${esc(target.group)}</small></th>
          <td>${badge(target.status)}</td><td>${number(target.rtt)}</td><td>${number(target.stddev)}</td>
          <td>${target.loss.loss == null ? "N/A" : `${number(target.loss.loss, 1)}%`}</td>
        </tr>`).join("")}</tbody>`;
    }).join("");
    return `<p class="overview-note overview-table-hint" id="overviewGatewayScrollHint">横向滑动或聚焦表格后按方向键，可查看全部列。</p>
    <div class="overview-table-wrap" tabindex="0" role="region" aria-labelledby="overviewGatewayTitle"
      aria-describedby="overviewGatewayScrollHint"><table><thead><tr>
      <th scope="col">Name</th><th scope="col">Status</th><th scope="col">RTT <small>ms</small></th>
      <th scope="col">STDDEV <small>ms</small></th><th scope="col">Loss <small>%</small></th>
    </tr></thead>${groups}</table></div>
    ${state.moreGateways ? `<p class="overview-note">另有 ${state.moreGateways} 个网关探测目标见 <a href="/ping">Ping 监控</a>。</p>` : ""}`;
  }

  function createOverviewView(root) {
    const select = (part) => root.querySelector(`[data-overview="${part}"]`);
    const rendered = new Map();
    root.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      event.target.closest("tr[data-gateway]")?.querySelector("a")?.click();
    });
    function html(part, markup) {
      const element = select(part);
      if (rendered.get(part) === markup) return;
      const previousTable = element.querySelector(".overview-table-wrap");
      const tableScroll = previousTable && { left: previousTable.scrollLeft, top: previousTable.scrollTop };
      const active = document.activeElement;
      const focused = element.contains(active) ? active.getAttribute("href") : null;
      const tableFocused = previousTable && previousTable === active;
      element.innerHTML = markup;
      rendered.set(part, markup);
      const table = element.querySelector(".overview-table-wrap");
      if (tableScroll && table) {
        table.scrollTo(tableScroll);
        if (tableFocused) table.focus({ preventScroll: true });
      }
      if (focused) [...element.querySelectorAll("a")].find((link) => link.getAttribute("href") === focused)?.focus({ preventScroll: true });
    }
    return function render(state, meta) {
      const health = select("health");
      health.dataset.health = state.health;
      health.dataset.availability = state.availability;
      const healthText = state.health === "fault" && state.availability !== "ready"
        ? `${healthLabels.fault} · ${healthLabels[state.availability]}` : healthLabels[state.health];
      health.textContent = meta.pingState === "loading" ? "正在读取监控" : healthText;
      const infra = state.infraCounts;
      const isp = state.ispCounts;
      html("infra", `<b>${infra.UP + infra.DEGRADED} 在线</b> · <b>${infra.DOWN} 离线</b> · <b>${infra.UNKNOWN} 未知</b>`);
      select("isp").textContent = `${isp.UP + isp.DEGRADED} / ${count(isp)} 在线`;
      select("quality").textContent = String(infra.DEGRADED + isp.DEGRADED);
      select("updated").textContent = meta.lastProbe === null ? "—" : formatTime.format(new Date(meta.lastProbe * 1000));
      const coverage = [
        state.agedOut ? `${state.agedOut} 个目标已超出观察窗口` : "",
        state.uncovered ? `${state.uncovered} 个 WAN 网关缺少探测覆盖` : "",
        state.unconfigured ? `${state.unconfigured} 个 WAN 未提供网关，探测关联待核实` : "",
        state.ambiguous ? `${state.ambiguous} 个 WAN 探测关联有歧义` : "",
        state.missingGroups ? "Core 或 ISP 网关覆盖不足" : "",
        state.missingFirewallVips ? `${state.missingFirewallVips} 个已配置的 FW VIP 缺少探测覆盖` : "",
        state.firewallConfigInvalid ? "FW VIP 探测配置无效" : "",
        state.incomplete ? "部分关键目标 RTT 或 Loss 样本不足" : "",
        state.statsUnavailable ? "STDDEV 查询不可用" : "",
        state.wanUnavailable && meta.wanState !== "loading" ? "WAN 采集缺失或部分不可用" : ""
      ].filter(Boolean).join(" · ");
      select("coverage").textContent = coverage;
      select("wan-source").textContent = meta.wanState === "loading" ? "正在读取 WAN" :
        state.wanUnavailable ? "WAN 采集缺失或部分不可用" : "IF-MIB · 近 15 分钟";
      html("bandwidth", state.wans.length ? state.wans.map(bandwidthCard).join("") :
        `<p class="overview-empty">${meta.wanState === "loading" ? "正在读取已发现 WAN 接口…" : "WAN 清单为空或暂不可用。"}</p>`);
      if (business && select("bc-ob") && select("bc-vlan")) {
        html("bc-ob", business.render(meta.bcData?.remote_ob, meta.bcState, "remote_ob"));
        html("bc-vlan", business.render(meta.bcData?.business_vlan, meta.bcState, "business_vlan"));
      }
      html("fault-summary", Object.entries(faultLabels).map(([status, label]) =>
        `<span class="overview-badge" data-status="${status}" data-count="${state.total[status]}">${label} ${state.total[status]}</span>`).join(""));
      html("faults", faultsMarkup(state));
      html("gateways", gatewayMarkup(state));
    };
  }

  window.BSOverviewView = { createOverviewView };
})();
