;(function () {
  'use strict';

  const { escapeHtml: esc } = window.BSUtils;
  const { pingHref } = window.BSOverviewModel;
  const maxFaults = 8;
  const healthLabels = { healthy: "All Good", fault: "检测到网络异常", partial: "监控部分不可用", unavailable: "监控不可用" };
  const formatTime = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const number = (value, places = 2) => value == null ? "N/A" : value.toFixed(places);
  const count = (values) => values.UP + values.DOWN + values.DEGRADED + values.UNKNOWN;

  function targetLink(target) {
    return `<a href="${pingHref(target)}">${esc(target.name)}</a>`;
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
      <path d="M0 24H200" class="overview-baseline" />
      <path d="${path(series[0])}" class="overview-rx" /><path d="${path(series[1])}" class="overview-tx" />
    </svg>`;
  }

  function direction(data, label, color) {
    const valid = data.bps !== null;
    const mbps = valid ? data.bps / 1e6 : null;
    const percent = valid && data.capacity ? mbps * 100 / data.capacity : null;
    return `<div class="overview-rate" data-direction="${color}" title="${esc(data.reason)}">
      <span>${label}</span><strong>${number(mbps, 1)}${valid ? ' <small>Mbps</small>' : ""}</strong>
      ${percent === null ? "" : `<div class="overview-util" aria-label="占配置容量 ${number(percent, 1)}%">
        <i style="width:${Math.min(100, percent)}%"></i></div>
        <small>${number(percent, 1)}% / ${data.capacity} Mbps</small>`}
    </div>`;
  }

  function bandwidthCard(card) {
    const warning = card.in.reason || card.out.reason;
    const identity = card.instance && card.index ? `${card.instance} · ifIndex ${card.index}` : "接口待核实";
    const capacity = card.in.capacity !== null || card.out.capacity !== null ? "配置线路容量" :
      card.ambiguousName ? "同名接口 · 容量待核实" : "容量待核实";
    return `<article class="overview-wan" data-wan-key="${esc(card.key || "")}">
      <h3 title="${esc(identity)}">${esc(card.name)}</h3>
      <p class="overview-wan-id">${esc(identity)}</p>
      <div class="overview-rates">${direction(card.in, "入站 ↓", "in")}${direction(card.out, "出站 ↑", "out")}</div>
      ${sparkline(card)}
      <p class="overview-wan-note${warning ? " overview-warning" : ""}">${esc(warning || capacity)}</p>
    </article>`;
  }

  function faultsMarkup(state) {
    const rows = state.faults.slice(0, maxFaults).map((target) => `<li>
      <div>${targetLink(target)} <small>${esc(target.group)} · ${esc(target.ip)}</small></div>
      ${badge(target.status)}<small>${target.status === "DOWN" ? "起始未知 · 时长未知" : esc(target.reason || "网络质量超阈值")}</small>
    </li>`).join("");
    const clients = state.clients.map((client) => {
      const category = client.category === "ap" ? "AP ICMP" : "选手";
      return `<a href="/ping">${category} <strong>${client.DOWN} 离线</strong> · ${client.DEGRADED} 异常 · ${client.UNKNOWN} 未知</a>`;
    }).join("");
    const blank = state.health === "healthy" ? "关键目标状态新鲜，WAN 两个方向均有数据。" : "暂无明确的基础设施故障记录。请核对覆盖和数据源。";
    return `${rows ? `<ul class="overview-faults">${rows}</ul>` : `<p class="overview-empty">${blank}</p>`}
      ${state.faults.length > maxFaults ? '<p class="overview-note">其余异常见 <a href="/ping">完整 Ping 监控</a>。</p>' : ""}
      <div class="overview-client-counts">${clients}</div>`;
  }

  function gatewayMarkup(state) {
    if (!state.gatewayRows.length) return '<p class="overview-empty">暂无可用的网关探测目标。</p>';
    return `<div class="overview-table-wrap" tabindex="0" aria-label="滚动查看网关状态"><table><thead><tr>
      <th scope="col">Name</th><th scope="col">Status</th><th scope="col">RTT <small>ms</small></th>
      <th scope="col">STDDEV <small>ms</small></th><th scope="col">Loss <small>%</small></th>
    </tr></thead><tbody>${state.gatewayRows.map((target) => `<tr data-gateway>
      <th scope="row">${targetLink(target)} <small>${esc(target.group)} · ${esc(target.ip)}</small></th>
      <td>${badge(target.status)}</td><td>${number(target.rtt)}</td><td>${number(target.stddev)}</td>
      <td>${target.loss.loss == null ? "N/A" : `${number(target.loss.loss, 1)}%`}</td>
    </tr>`).join("")}</tbody></table></div>
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
      const focused = element.contains(document.activeElement) ? document.activeElement.getAttribute("href") : null;
      element.innerHTML = markup;
      rendered.set(part, markup);
      if (focused) [...element.querySelectorAll("a")].find((link) => link.getAttribute("href") === focused)?.focus({ preventScroll: true });
    }
    return function render(state, meta) {
      const health = select("health");
      health.dataset.health = state.health;
      health.textContent = meta.pingState === "loading" ? "正在读取监控" : healthLabels[state.health];
      const infra = state.infraCounts;
      const isp = state.ispCounts;
      html("infra", `<b>${infra.UP + infra.DEGRADED} 在线</b> · <b>${infra.DOWN} 离线</b> · <b>${infra.UNKNOWN} 未知</b>`);
      select("isp").textContent = `${isp.UP + isp.DEGRADED} / ${count(isp)} 在线`;
      select("quality").textContent = String(infra.DEGRADED + isp.DEGRADED);
      select("updated").textContent = meta.lastProbe === null ? "—" : formatTime.format(new Date(meta.lastProbe * 1000));
      const coverage = [
        "目标清单采用近 30 分钟抓取记录",
        state.agedOut ? `${state.agedOut} 个目标已超出观察窗口` : "",
        state.uncovered ? `${state.uncovered} 个 WAN 网关缺少探测覆盖` : "",
        state.missingGroups ? "Core 或 ISP 网关覆盖不足" : "",
        state.incomplete ? "部分关键目标 RTT 或 Loss 样本不足" : "",
        state.statsUnavailable ? "STDDEV 查询不可用" : ""
      ].filter(Boolean).join(" · ");
      select("coverage").textContent = coverage;
      select("wan-source").textContent = meta.wanState === "loading" ? "正在读取 WAN" :
        state.wanUnavailable ? "WAN 采集缺失或部分不可用" : "IF-MIB · 近 15 分钟";
      html("bandwidth", state.wans.length ? state.wans.map(bandwidthCard).join("") :
        `<p class="overview-empty">${meta.wanState === "loading" ? "正在读取已发现 WAN 接口…" : "WAN 清单为空或暂不可用。"}</p>`);
      html("faults", faultsMarkup(state));
      html("gateways", gatewayMarkup(state));
    };
  }

  window.BSOverviewView = { createOverviewView };
})();
