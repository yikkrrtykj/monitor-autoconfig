;(function () {
  'use strict';

  function haRows(inspector) {
    const ha = inspector.ha;
    if (!ha || ha.source !== 'Hillstone sysHAStatus' || !Array.isArray(ha.units) || !ha.units.length) {
      return [['HA 角色', '未知（暂无可信数据源）']];
    }
    const states = { none: 'None', init: 'Init', hello: 'Hello', backup: 'Backup', master: 'Master', 'AA-mode': 'AA-mode' };
    const units = ha.units.slice(0, 16).filter((unit) => unit && typeof unit === 'object');
    const stateText = (unit) => {
      const state = unit.fresh === true && Object.prototype.hasOwnProperty.call(states, unit.state) ? states[unit.state] : null;
      return state || (unit.fresh === true && unit.state === 'vendor-unknown' ? '未知（厂商 slase / 5）' : '未知');
    };
    const summary = (items) => items.map((unit) => `${unit.name || unit.ip || '单元'} ${stateText(unit)}`).join(' · ');
    const selected = units.filter((unit) => unit.ip === inspector.ip);
    if (selected.length) {
      const rows = [['HA 角色', selected.length === 1 ? stateText(selected[0]) : '未知']];
      const peers = units.filter((unit) => unit.ip !== inspector.ip);
      if (peers.length) rows.push(['HA 对端', summary(peers)]);
      return rows;
    }
    return [['HA 集群', summary(units) || '未知（暂无可信数据源）']];
  }

  function createTopologyPanel(dependencies) {
    const {
      document,
      location,
      buildTopologyLayers,
      topologyLayout,
      renderTopologySvg,
      topologyNodeKindLabel,
      topologyLatencyIp,
      escapeHtml,
      formatPingText,
      fetchNodeInspector,
      fetchNodePorts,
      portPanel
    } = dependencies;

    let apVisible = false;
    const apStorage = () => dependencies.getSessionStorage ? dependencies.getSessionStorage() : dependencies.sessionStorage;
    try { apVisible = apStorage()?.getItem("topology-ap-visible") === "on"; } catch (error) {}
    const apToggle = document.getElementById("topologyApToggle");
    const apCount = document.getElementById("topologyApCount");
    const updateApControl = () => {
      if (apToggle) {
        apToggle.textContent = apVisible ? "AP: 显示" : "AP: 隐藏";
        apToggle.setAttribute("aria-pressed", String(apVisible));
      }
      if (apCount) apCount.hidden = !apVisible;
    };
    updateApControl();
    if (apToggle) apToggle.onclick = () => {
      apVisible = !apVisible;
      try { apStorage()?.setItem("topology-ap-visible", apVisible ? "on" : "off"); } catch (error) {}
      updateApControl();
      clearDetail(true);
      // Remove AP nodes immediately; an older pending response cannot restore them.
      if (!apVisible && lastWiredFrame) render(lastWiredFrame);
      if (dependencies.onApVisibilityChange) dependencies.onApVisibilityChange();
    };
    let lastWiredFrame = null;
    let topologyNodes = [];
    let inspectorRequest = 0;
    let portsRequest = 0;
    const topoView = { scale: 1, x: 0, y: 0 };

    const canvasElement = () => document.getElementById("topologyCanvas");
    const detailElement = () => document.getElementById("topologyDetail");

    function isAvailable() {
      return Boolean(canvasElement());
    }

    function closePorts() {
      portsRequest += 1;
      if (portPanel) portPanel.close();
    }

    if (portPanel) portPanel.setCloseHandler(() => { portsRequest += 1; });

    function bindTopologyNodeEvents() {
      const detail = detailElement();
      const canvas = canvasElement();
      if (canvas) {
        canvas.onclick = (event) => {
          if (event.target.closest && event.target.closest(".topology-node")) return;
          inspectorRequest += 1;
          closePorts();
          detail.hidden = true;
        };
      }
      document.querySelectorAll(".topology-node").forEach((el) => {
        const handler = (event) => {
          if (event && event.stopPropagation) event.stopPropagation();
          const idx = Number(el.dataset.idx);
          const node = topologyNodes[idx];
          if (!node) return;
          inspectorRequest += 1;
          closePorts();
          const syslogUrl = node.ip ? `${location.protocol}//${location.hostname}:3000/d/device-syslog?var-host=${encodeURIComponent(node.ip)}` : "";
          const latencyIp = topologyLatencyIp(node);
          const actions = `<div class="topology-detail-actions">
              ${latencyIp ? `<a class="detail-link" href="/latency?ip=${encodeURIComponent(latencyIp)}">延迟</a>` : ""}
              ${syslogUrl ? `<a class="detail-link" href="${escapeHtml(syslogUrl)}">Syslog</a>` : ""}
            </div>`;
          const header = (name) => `<header><strong>${escapeHtml(name)}</strong><button class="topology-detail-close" type="button" aria-label="关闭详情">×</button></header>`;
          const legacyCard = `${header(node.name)}
            <dl>
              ${node.kind === "ap" ? `<dt>Hostname</dt><dd>${escapeHtml(node.hostname || "—")}</dd>
              <dt>管理 IP</dt><dd>${escapeHtml(node.ip || "—")}</dd>
              <dt>型号</dt><dd>${escapeHtml(node.model || "—")}</dd>
              <dt>状态</dt><dd>${node.success === true ? "在线" : node.success === false ? "离线" : "未知"}</dd>
              <dt>延迟</dt><dd>${Number.isFinite(node.latency) ? formatPingText(node.latency) : "—"}</dd>
              <dt>VLAN</dt><dd>${node.vlan || "—"}</dd>
              <dt>上联端口</dt><dd>${escapeHtml(node.switchPort || "—")}</dd>
              <dt>客户端</dt><dd>${Number.isFinite(node.clients) ? node.clients : "—"}</dd>` : `<dt>类型</dt><dd>${escapeHtml(topologyNodeKindLabel(node.kind))}</dd>
              <dt>IP</dt><dd>${escapeHtml(node.ip || "—")}</dd>
              <dt>状态</dt><dd>${node.success === null ? "状态未知" : (node.success === undefined ? "无数据" : (node.success ? "在线" : "离线"))}</dd>
              <dt>延迟</dt><dd>${Number.isFinite(node.latency) ? formatPingText(node.latency) : "—"}</dd>
              `}
            </dl>${actions}`;
          const hasInspector = Boolean(node.ip && ["core", "dist", "device", "firewall", "ap"].includes(node.kind) && fetchNodeInspector);
          const bindClose = () => {
            detail.querySelector(".topology-detail-close").onclick = () => { inspectorRequest += 1; closePorts(); detail.hidden = true; };
          };
          detail.hidden = false;
          detail.innerHTML = hasInspector
            ? `${header(node.name)}<p class="topology-inspector-loading">节点详情读取中…</p>${actions}`
            : legacyCard;
          bindClose();
          if (!hasInspector) return;
          const request = inspectorRequest;
          fetchNodeInspector(node.ip).then((inspector) => {
            if (request !== inspectorRequest || detail.hidden) return;
            const isAp = node.kind === "ap" || inspector.kind === "unifi-ap";
            const ports = inspector.ports;
            const warnings = Array.isArray(inspector.warnings) ? inspector.warnings : [];
            const connections = inspector.connections;
            const hasConnectionCounts = connections && Number.isInteger(connections.peers) && connections.peers >= 0
              && Number.isInteger(connections.aggregates) && connections.aggregates >= 0;
            const connectionSummary = hasConnectionCounts && !warnings.includes("邻接资料暂不可用")
              ? `邻接 ${connections.peers} 台 · 聚合链路 ${connections.aggregates} 组` : "—";
            const state = inspector.online === "up" ? "在线" : inspector.online === "down" ? "离线" : "未知";
            const row = (label, value, haValue = false) => `<dt>${escapeHtml(label)}</dt><dd${haValue ? ' class="topology-ha-value"' : ''}>${escapeHtml(value == null || value === "" ? "—" : String(value))}</dd>`;
            detail.innerHTML = `
              ${header(inspector.name || node.name)}
              <dl>
                ${row("Hostname", inspector.hostname)}${row("管理 IP", inspector.ip)}${row("型号", inspector.model)}
                ${row("状态", state)}${row("延迟", Number.isFinite(inspector.latencySeconds) ? formatPingText(inspector.latencySeconds) : isAp && Number.isFinite(node.latency) ? formatPingText(node.latency) : null)}
                ${isAp ? row("VLAN", node.vlan) + row("上联端口", node.switchPort) + row("客户端", inspector.clients)
                  : row("端口", ports ? `在线 ${ports.up} / 离线 ${ports.down} / 未知 ${ports.unknown}` : null) + row("连接摘要", connectionSummary)}
                ${!isAp && inspector.kind === "hillstone" ? haRows(inspector).map(([label, value]) => row(label, value, true)).join('') : !isAp && node.kind === "firewall" ? row("HA 角色", "未知（暂无可信数据源）") : ""}
              </dl>
              ${warnings.length ? `<div class="topology-inspector-warnings">${warnings.map((warning) => `<p>${escapeHtml(warning)}</p>`).join("")}</div>` : ""}
              ${actions}
              ${!isAp && fetchNodePorts && portPanel && (["hillstone", "generic-switch"].includes(inspector.kind) ||
                (inspector.kind === "cisco" && ["core", "dist"].includes(node.kind)))
                ? `<div class="topology-detail-actions"><button type="button" class="topology-view-ports">${inspector.kind !== "cisco" ? "查看接口" : "查看端口"}</button></div>` : ''}
            `;
            bindClose();
            const viewPorts = detail.querySelector(".topology-view-ports");
            if (viewPorts) viewPorts.onclick = () => {
              const currentRequest = ++portsRequest;
              portPanel.loading(node.ip, inspector.kind);
              fetchNodePorts(node.ip).then((result) => {
                if (currentRequest !== portsRequest || detail.hidden || !portPanel.isOpen()) return;
                portPanel.open(result);
              }).catch((error) => {
                if (currentRequest !== portsRequest || detail.hidden || !portPanel.isOpen()) return;
                if (error.status === 401 || error.status === 403) { closePorts(); return; }
                portPanel.failure();
              });
            };
          }).catch((error) => {
            if (request !== inspectorRequest || detail.hidden) return;
            if (error.status === 401 || error.status === 403) {
              detail.innerHTML = legacyCard;
              bindClose();
              return;
            }
            detail.innerHTML = `${legacyCard}<p class="topology-inspector-warnings">节点详情暂不可用</p>`;
            bindClose();
          });
        };
        el.addEventListener("click", handler);
        el.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            handler(event);
          }
        });
      });
    }

    function applyTopoView() {
      const canvas = canvasElement();
      const svg = canvas && canvas.querySelector(".topology-svg");
      if (!svg) return;
      const baseWidth = Number(svg.dataset.baseWidth || 0);
      const baseHeight = Number(svg.dataset.baseHeight || 0);
      if (!baseWidth || !baseHeight) return;
      const viewWidth = baseWidth / topoView.scale;
      const viewHeight = baseHeight / topoView.scale;
      svg.setAttribute("viewBox", `${topoView.x} ${topoView.y} ${viewWidth} ${viewHeight}`);
    }

    function resetView() {
      topoView.scale = 1;
      topoView.x = 0;
      topoView.y = 0;
      applyTopoView();
    }

    // Drag to pan, wheel to zoom. Bound once on the canvas container so it
    // survives the 10s re-render; the transform itself is re-applied each refresh.
    function setupTopoPanZoom() {
      const canvas = canvasElement();
      if (!canvas || canvas.dataset.panzoom === "1") return;
      canvas.dataset.panzoom = "1";

      let pointerDown = false;
      let dragging = false;
      let moved = false;
      let startX = 0;
      let startY = 0;
      let originX = 0;
      let originY = 0;
      let originScale = 1;
      let activePointer = null;

      canvas.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        pointerDown = true;
        dragging = false;
        moved = false;
        startX = event.clientX;
        startY = event.clientY;
        originX = topoView.x;
        originY = topoView.y;
        originScale = topoView.scale;
        activePointer = event.pointerId;
        // Don't capture or preventDefault yet — a plain click must still reach the node.
      });

      canvas.addEventListener("pointermove", (event) => {
        if (!pointerDown) return;
        const dx = event.clientX - startX;
        const dy = event.clientY - startY;
        if (!dragging && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
          dragging = true;
          moved = true;
          canvas.classList.add("topology-grabbing");
          try { canvas.setPointerCapture(activePointer); } catch (e) {}
        }
        if (!dragging) return;
        const svg = canvas.querySelector(".topology-svg");
        const baseWidth = Number(svg && svg.dataset.baseWidth || 0);
        const baseHeight = Number(svg && svg.dataset.baseHeight || 0);
        const rect = canvas.getBoundingClientRect();
        if (!baseWidth || !baseHeight || !rect.width || !rect.height) return;
        topoView.x = originX - dx * (baseWidth / originScale) / rect.width;
        topoView.y = originY - dy * (baseHeight / originScale) / rect.height;
        applyTopoView();
      });

      const endDrag = () => {
        if (!pointerDown) return;
        pointerDown = false;
        if (dragging) {
          canvas.classList.remove("topology-grabbing");
          try { canvas.releasePointerCapture(activePointer); } catch (e) {}
        }
        dragging = false;
      };
      canvas.addEventListener("pointerup", endDrag);
      canvas.addEventListener("pointercancel", endDrag);

      // If the pointer actually dragged, swallow the trailing click so it neither
      // clears the detail panel nor opens a node.
      canvas.addEventListener("click", (event) => {
        if (moved) {
          event.stopPropagation();
          moved = false;
        }
      }, true);

      canvas.addEventListener("wheel", (event) => {
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const svg = canvas.querySelector(".topology-svg");
        const baseWidth = Number(svg && svg.dataset.baseWidth || 0);
        const baseHeight = Number(svg && svg.dataset.baseHeight || 0);
        if (!baseWidth || !baseHeight || !rect.width || !rect.height) return;
        const cx = event.clientX - rect.left;
        const cy = event.clientY - rect.top;
        const viewWidth = baseWidth / topoView.scale;
        const viewHeight = baseHeight / topoView.scale;
        const focusX = topoView.x + (cx / rect.width) * viewWidth;
        const focusY = topoView.y + (cy / rect.height) * viewHeight;
        const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
        const next = Math.min(4, Math.max(0.3, topoView.scale * factor));
        topoView.scale = next;
        topoView.x = focusX - (cx / rect.width) * (baseWidth / topoView.scale);
        topoView.y = focusY - (cy / rect.height) * (baseHeight / topoView.scale);
        applyTopoView();
      }, { passive: false });

      canvas.addEventListener("dblclick", resetView);
      // Belt-and-suspenders: stop the browser from drag-selecting the SVG labels.
      canvas.addEventListener("selectstart", (event) => event.preventDefault());
      canvas.addEventListener("dragstart", (event) => event.preventDefault());
    }

    function prepare(targets, edges, apData) {
      const canvas = canvasElement();
      const layers = buildTopologyLayers(targets);
      const containerWidth = Math.max(640, canvas.clientWidth || 1200);
      const height = Math.max(420, canvas.clientHeight || 680);
      // Lay the graph out at its natural width so a long row of access switches
      // doesn't get squeezed/overlapped; pan & zoom let you explore the rest.
      const maxRow = Math.max(
        layers.isps.length, layers.firewalls.length,
        layers.cores.length,
        // Attached servers can share the same downstream row as access
        // switches, so reserve width for both populations together.
        layers.dists.length + layers.servers.length,
        1
      );
      const width = Math.max(containerWidth, maxRow * 168 + 48);
      const layout = topologyLayout(layers, width, height, edges);
      lastWiredFrame = { layout, width };
      const frame = apVisible && dependencies.appendApLeaves ? dependencies.appendApLeaves(lastWiredFrame, apData) : lastWiredFrame;
      if (apCount && apVisible) apCount.textContent = `AP ${frame.apCount?.located || 0}/${frame.apCount?.total || 0} 已定位`;
      return frame;
    }

    function render(frame) {
      const canvas = canvasElement();
      topologyNodes = frame.layout.nodes;
      // Keep the original wired SVG viewport, aspect ratio, and pan/zoom. APs
      // overflow into extra scroll space; enlarging the viewBox would re-fit it.
      canvas.innerHTML = `<div class="topology-additive-frame">${renderTopologySvg(frame.layout, frame.width)}</div>`;
      const svg = canvas.querySelector(".topology-svg");
      const holder = canvas.querySelector(".topology-additive-frame");
      const viewportHeight = Math.max(420, canvas.clientHeight || 680);
      if (svg?.style) svg.style.height = `${viewportHeight}px`;
      if (holder?.style) {
        const fit = Math.min((canvas.clientWidth || 1200) / frame.width, viewportHeight / (frame.layout.wiredHeight || frame.layout.height));
        holder.style.height = `${viewportHeight + Math.max(0, frame.layout.height - (frame.layout.wiredHeight || frame.layout.height)) * fit * topoView.scale}px`;
      }
      bindTopologyNodeEvents();
      setupTopoPanZoom();
      applyTopoView();
    }

    function updateLatency(nodes) {
      const canvas = canvasElement();
      topologyNodes = nodes;
      canvas.querySelectorAll(".topology-node").forEach((el) => {
        const node = topologyNodes[Number(el.dataset.idx)];
        const text = el.querySelector(".topology-node-latency");
        if (!node || !text) return;
        if (node.kind === "ap") {
          const ping = Number.isFinite(node.latency) ? formatPingText(node.latency) : node.success === true ? "在线" : node.success === false ? "离线" : "未知";
          text.textContent = `${ping}${node.vlan ? ` · VLAN ${node.vlan}` : ""}`;
          return;
        }
        text.textContent = node.success === null ? "状态未知" : Number.isFinite(node.latency)
          ? formatPingText(node.latency)
          : (node.kind === "isp" && node.success === true ? "在线" : "");
      });
    }

    function updateStatus(edges) {
      document.getElementById("topologyUpdated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 拖动平移·滚轮缩放·双击复位${edges.length ? ` · LLDP ${edges.length} 条链路` : " · LLDP 未发现邻居"}`;
    }

    function showError(message) {
      lastWiredFrame = null;
      canvasElement().innerHTML = `<div class="topology-error">拓扑数据拉取失败: ${escapeHtml(message || "")}</div>`;
    }

    function clearDetail(preserveWired = false) {
      if (!preserveWired) lastWiredFrame = null;
      inspectorRequest += 1;
      closePorts();
      const detail = detailElement();
      detail.hidden = true;
      detail.innerHTML = `<div class="topology-empty">点击任意节点查看详情</div>`;
    }


    return {
      isAvailable,
      isApVisible: () => apVisible,
      prepare,
      render,
      updateLatency,
      updateStatus,
      showError,
      clearDetail,
      resetView
    };
  }

  const ns = { createTopologyPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSTopologyPanel = ns;
  }
}());
