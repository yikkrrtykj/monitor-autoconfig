;(function () {
  'use strict';

  function createTopologyPanel(dependencies) {
    const {
      document,
      location,
      buildTopologyLayers,
      topologyLayout,
      renderTopologySvg,
      projectPhysicalTopology,
      physicalTopologyLayout,
      renderPhysicalTopologySvg,
      topologyNodeKindLabel,
      topologyLatencyIp,
      escapeHtml,
      formatPingText,
      onModeChange
    } = dependencies;

    let topologyNodes = [];
    let topologyMode = "operations";
    let physicalFactCount = 0;
    const topoView = { scale: 1, x: 0, y: 0 };

    const canvasElement = () => document.getElementById("topologyCanvas");
    const detailElement = () => document.getElementById("topologyDetail");

    function isAvailable() {
      return Boolean(canvasElement());
    }

    function updateModeControls() {
      ["operations", "physical"].forEach((mode) => {
        const button = document.getElementById(
          mode === "operations" ? "topologyViewOperations" : "topologyViewPhysical"
        );
        if (!button) return;
        button.setAttribute("aria-pressed", mode === topologyMode ? "true" : "false");
      });
      const canvas = canvasElement();
      if (canvas) canvas.dataset.topologyView = topologyMode;
    }

    function getMode() {
      return topologyMode;
    }

    function setMode(mode, notify = true) {
      if (mode !== "operations" && mode !== "physical") return false;
      if (mode === topologyMode) {
        updateModeControls();
        return false;
      }
      topologyMode = mode;
      updateModeControls();
      clearDetail();
      resetView();
      if (notify && typeof onModeChange === "function") onModeChange(mode);
      return true;
    }

    function bindModeControls() {
      const operations = document.getElementById("topologyViewOperations");
      const physical = document.getElementById("topologyViewPhysical");
      if (operations && operations.dataset.bound !== "1") {
        operations.addEventListener("click", () => setMode("operations"));
        operations.dataset.bound = "1";
      }
      if (physical && physical.dataset.bound !== "1") {
        physical.addEventListener("click", () => setMode("physical"));
        physical.dataset.bound = "1";
      }
      updateModeControls();
    }

    function bindTopologyNodeEvents() {
      const detail = detailElement();
      const canvas = canvasElement();
      if (canvas) {
        canvas.onclick = (event) => {
          if (event.target.closest && event.target.closest(".topology-node")) return;
          detail.hidden = true;
        };
      }
      document.querySelectorAll(".topology-node").forEach((el) => {
        const handler = (event) => {
          if (event && event.stopPropagation) event.stopPropagation();
          const idx = Number(el.dataset.idx);
          const node = topologyNodes[idx];
          if (!node) return;
          const syslogUrl = node.ip ? `${location.protocol}//${location.hostname}:3000/d/device-syslog?var-host=${encodeURIComponent(node.ip)}` : "";
          const latencyIp = topologyLatencyIp(node);
          detail.hidden = false;
          detail.innerHTML = `
            <header><strong>${escapeHtml(node.name)}</strong><span class="dot ${node.level}"></span></header>
            <dl>
              <dt>类型</dt><dd>${escapeHtml(topologyNodeKindLabel(node.kind))}</dd>
              <dt>IP</dt><dd>${escapeHtml(node.ip || "—")}</dd>
              <dt>状态</dt><dd>${node.success === undefined ? "无数据" : (node.success ? "在线" : "离线")}</dd>
              <dt>延迟</dt><dd>${Number.isFinite(node.latency) ? formatPingText(node.latency) : "—"}</dd>
            </dl>
            <div class="topology-detail-actions">
              ${latencyIp ? `<a class="detail-link" href="/latency?ip=${encodeURIComponent(latencyIp)}">延迟</a>` : ""}
              ${syslogUrl ? `<a class="detail-link" href="${escapeHtml(syslogUrl)}">Syslog</a>` : ""}
            </div>
          `;
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

    function prepare(targets, edges) {
      const canvas = canvasElement();
      const containerWidth = Math.max(640, canvas.clientWidth || 1200);
      const height = Math.max(420, canvas.clientHeight || 680);
      if (topologyMode === "physical") {
        const projection = projectPhysicalTopology(edges);
        physicalFactCount = projection.physicalLinks.length +
          projection.bundles.length + projection.serverAttachments.length;
        const layout = physicalTopologyLayout(projection, targets, containerWidth, height);
        return { layout, width: layout.width || containerWidth };
      }
      const layers = buildTopologyLayers(targets);
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
      return { layout, width };
    }

    function render(frame) {
      const canvas = canvasElement();
      topologyNodes = frame.layout.nodes;
      canvas.dataset.topologyView = topologyMode;
      canvas.innerHTML = topologyMode === "physical"
        ? renderPhysicalTopologySvg(frame.layout, frame.width)
        : renderTopologySvg(frame.layout, frame.width);
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
        text.textContent = Number.isFinite(node.latency)
          ? formatPingText(node.latency)
          : (node.kind === "isp" && node.success === true ? "在线" : "");
      });
    }

    function updateStatus(edges) {
      if (topologyMode === "physical") {
        document.getElementById("topologyUpdated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 拖动平移·滚轮缩放·双击复位${physicalFactCount ? ` · Physical ${physicalFactCount} 条链路` : " · No accepted physical topology"}`;
        return;
      }
      document.getElementById("topologyUpdated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 拖动平移·滚轮缩放·双击复位${edges.length ? ` · LLDP ${edges.length} 条边` : " · LLDP 未发现邻居"}`;
    }

    function showError(message) {
      canvasElement().innerHTML = `<div class="topology-error">拓扑数据拉取失败: ${escapeHtml(message || "")}</div>`;
    }

    function clearDetail() {
      const detail = detailElement();
      detail.hidden = true;
      detail.innerHTML = `<div class="topology-empty">点击任意节点查看详情</div>`;
    }

    bindModeControls();

    return {
      isAvailable,
      prepare,
      render,
      updateLatency,
      updateStatus,
      showError,
      clearDetail,
      resetView,
      getMode,
      setMode
    };
  }

  const ns = { createTopologyPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSTopologyPanel = ns;
  }
}());
