;(function () {
  'use strict';

  function createIperfController(dependencies) {
    const {
      document,
      window,
      fetch,
      escapeHtml,
      postPlatform,
      fetchIperfStatus,
      fetchIperfHistory,
      defaultCustomPreset,
      resultView,
      historyHtml,
      loadServerConfig,
      presetView
    } = dependencies;

    let iperfPresets = { custom: defaultCustomPreset };
    let pendingIperfRequest = null;
    let activeIperfTaskId = "";
    let iperfProgressTimer = null;
    let iperfProgressRefreshing = false;
    let mounted = false;
    const iperfTaskStorageKey = "bigscreen.iperfTaskId";

    function markup() {
      return `
        <section class="network-tool" aria-labelledby="iperfToolTitle">
          <div class="network-tool-heading">
            <div>
              <h3 id="iperfToolTitle">iPerf3 出口测速</h3>
              <p>默认使用香港公共节点；公共节点繁忙时会自动尝试同组其他端口。</p>
            </div>
            <span class="network-tool-badge">主动占用带宽</span>
          </div>
          <div class="network-tool-grid iperf-tool-grid">
            <label>测速地区
              <select id="iperfPreset">
                <option value="hongkong" selected>中国香港（公共节点）</option>
                <option value="singapore">新加坡（公共节点）</option>
                <option value="istanbul">土耳其·伊斯坦布尔（公共节点）</option>
                <option value="indonesia">印度尼西亚（公共节点）</option>
                <option value="custom">自定义</option>
              </select>
            </label>
            <label>公共服务器
              <select id="iperfPublicServer"></select>
            </label>
            <label>服务器
              <input id="iperfServer" type="text" placeholder="正在加载版本化节点配置" spellcheck="false" readonly />
            </label>
            <label>端口或范围
              <input id="iperfPorts" type="text" inputmode="numeric" spellcheck="false" readonly />
            </label>
            <label>单向时长（秒）
              <input id="iperfDuration" type="text" inputmode="numeric" value="10" spellcheck="false" />
            </label>
            <label>并发连接
              <input id="iperfParallel" type="text" inputmode="numeric" value="10" spellcheck="false" />
            </label>
            <label>方向
              <select id="iperfDirection">
                <option value="both" selected>先上传，再下载</option>
                <option value="upload">仅上传</option>
                <option value="download">仅下载</option>
              </select>
            </label>
          </div>
          <p class="network-tool-hint" id="iperfPresetHint">香港 Leaseweb 公共节点；共享服务器繁忙时结果可能偏低。</p>
          <div class="network-tool-actions">
            <button type="button" class="delivery-test-alert" id="iperfRunBtn">开始测速</button>
            <button type="button" class="delivery-test-alert danger" id="iperfStopBtn" hidden>停止当前测速</button>
            <span>正常双向约 20 秒；节点繁忙时会重试，最长约 60 秒。</span>
          </div>
          <div class="iperf-confirm" id="iperfConfirm" hidden>
            <div class="iperf-confirm-copy">
              <strong>确认开始出口测速</strong>
              <span id="iperfConfirmSummary"></span>
            </div>
            <div class="iperf-confirm-actions">
              <button type="button" id="iperfCancelBtn">取消</button>
              <button type="button" class="primary" id="iperfConfirmBtn">确认并开始</button>
            </div>
          </div>
          <div class="iperf-progress" id="iperfProgress" hidden aria-live="polite">
            <div class="iperf-progress-heading">
              <strong id="iperfProgressPhase">准备测速</strong>
              <span id="iperfProgressElapsed">0.0 秒</span>
            </div>
            <div class="iperf-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
              <i id="iperfProgressFill"></i>
            </div>
            <span id="iperfProgressDetail">正在建立任务…</span>
          </div>
          <div class="network-tool-result" id="iperfResult" hidden></div>
          <div class="network-tool-history" id="iperfHistory" aria-live="polite"></div>
        </section>
      `;
    }

    function ensureMounted(container) {
      if (mounted || !container) return;
      mounted = true;
      container.insertAdjacentHTML("beforeend", markup());

      const iperfPreset = document.getElementById("iperfPreset");
      const iperfPublicServer = document.getElementById("iperfPublicServer");
      const iperfServer = document.getElementById("iperfServer");
      const iperfPorts = document.getElementById("iperfPorts");
      const iperfHint = document.getElementById("iperfPresetHint");
      const iperfBtn = document.getElementById("iperfRunBtn");
      const iperfConfirm = document.getElementById("iperfConfirm");
      const iperfConfirmSummary = document.getElementById("iperfConfirmSummary");
      const iperfConfirmBtn = document.getElementById("iperfConfirmBtn");
      const iperfCancelBtn = document.getElementById("iperfCancelBtn");
      const iperfStopBtn = document.getElementById("iperfStopBtn");
      const iperfProgress = document.getElementById("iperfProgress");
      const iperfProgressPhase = document.getElementById("iperfProgressPhase");
      const iperfProgressElapsed = document.getElementById("iperfProgressElapsed");
      const iperfProgressFill = document.getElementById("iperfProgressFill");
      const iperfProgressDetail = document.getElementById("iperfProgressDetail");
      const iperfHistory = document.getElementById("iperfHistory");

      const applyIperfPublicServer = () => {
        const view = presetView(iperfPresets, iperfPreset.value, iperfPublicServer.value);
        if (!view.server) return;
        iperfServer.value = view.server;
        iperfPorts.value = view.ports;
      };
      const applyIperfPreset = () => {
        const view = presetView(iperfPresets, iperfPreset.value);
        iperfServer.placeholder = view.placeholder;
        iperfServer.readOnly = !view.isCustom;
        iperfPorts.readOnly = !view.isCustom;
        if (view.isCustom) {
          iperfPublicServer.innerHTML = '<option value="0">手工填写</option>';
          iperfPublicServer.disabled = true;
          iperfServer.value = "";
          iperfPorts.value = view.ports;
        } else {
          iperfPublicServer.disabled = false;
          iperfPublicServer.innerHTML = view.options.map((item) => (
            `<option value="${item.index}">${escapeHtml(item.label)}</option>`
          )).join("");
          iperfServer.value = view.server;
          iperfPorts.value = view.ports;
        }
        if (iperfHint) iperfHint.textContent = view.note;
      };

      const hideIperfConfirmation = () => {
        pendingIperfRequest = null;
        if (iperfConfirm) iperfConfirm.hidden = true;
      };

      const renderIperfProgress = (status) => {
        if (!iperfProgress || !status || status.state === "unavailable") return;
        const elapsed = Math.max(0, Number(status.elapsedSeconds || 0));
        const maxSeconds = Math.max(1, Number(status.maxSeconds || 60));
        const reported = Math.max(0, Math.min(100, Number(status.percent || 0)));
        const timeFloor = status.state === "running" ? Math.min(95, (elapsed / maxSeconds) * 100) : 0;
        const percent = status.state === "complete" ? 100 : Math.max(reported, timeFloor);
        const phaseLabels = {
          preparing: "准备测速",
          upload: "上传测速",
          download: "下载测速",
          complete: "测速完成",
          failed: "测速失败",
          cancelled: "测速已停止"
        };
        iperfProgress.hidden = false;
        iperfProgress.className = `iperf-progress ${status.state || "running"}`;
        if (iperfProgressPhase) iperfProgressPhase.textContent = phaseLabels[status.phase] || "测速进行中";
        if (iperfProgressElapsed) iperfProgressElapsed.textContent = `${elapsed.toFixed(1)} 秒 / 最长 ${maxSeconds} 秒`;
        if (iperfProgressFill) iperfProgressFill.style.width = `${percent.toFixed(1)}%`;
        const track = iperfProgress.querySelector("[role=progressbar]");
        if (track) track.setAttribute("aria-valuenow", String(Math.round(percent)));
        if (iperfProgressDetail) iperfProgressDetail.textContent = status.message || "测速进行中";
      };

      const renderIperfTaskResult = (response) => {
        const result = document.getElementById("iperfResult");
        if (!result) return;
        result.hidden = false;
        const view = resultView(response, escapeHtml);
        result.className = view.className;
        if (Object.prototype.hasOwnProperty.call(view, "html")) result.innerHTML = view.html;
        else result.textContent = view.text;
      };

      const renderIperfHistory = (payload) => {
        if (!iperfHistory) return;
        iperfHistory.innerHTML = historyHtml(payload, escapeHtml);
      };

      const refreshIperfHistory = async () => renderIperfHistory(await fetchIperfHistory());

      const refreshIperfProgress = async () => {
        if (iperfProgressRefreshing) return;
        iperfProgressRefreshing = true;
        try {
          const status = await fetchIperfStatus(activeIperfTaskId);
          renderIperfProgress(status);
          if (status.state === "unavailable" && /不存在|过期/.test(status.error || "")) {
            if (iperfProgressTimer) window.clearInterval(iperfProgressTimer);
            iperfProgressTimer = null;
            window.sessionStorage.removeItem(iperfTaskStorageKey);
            activeIperfTaskId = "";
            iperfBtn.disabled = false;
            if (iperfStopBtn) iperfStopBtn.hidden = true;
            renderIperfTaskResult({ state: "failed", message: status.error });
            return;
          }
          if (["complete", "failed", "cancelled"].includes(status.state)) {
            if (iperfProgressTimer) window.clearInterval(iperfProgressTimer);
            iperfProgressTimer = null;
            window.sessionStorage.removeItem(iperfTaskStorageKey);
            activeIperfTaskId = "";
            iperfBtn.disabled = false;
            if (iperfStopBtn) iperfStopBtn.hidden = true;
            renderIperfTaskResult(status);
            refreshIperfHistory();
          }
        } finally {
          iperfProgressRefreshing = false;
        }
      };

      const startIperfProgress = () => {
        if (iperfProgressTimer) window.clearInterval(iperfProgressTimer);
        renderIperfProgress({
          state: "running",
          phase: "preparing",
          percent: 0,
          elapsedSeconds: 0,
          maxSeconds: 60,
          message: "正在连接测速服务…"
        });
        if (iperfStopBtn) iperfStopBtn.hidden = false;
        iperfProgressTimer = window.setInterval(refreshIperfProgress, 500);
      };

      const executeIperfTest = async (request) => {
        const result = document.getElementById("iperfResult");
        hideIperfConfirmation();
        iperfBtn.disabled = true;
        if (result) {
          result.hidden = false;
          result.className = "network-tool-result loading";
          result.textContent = "正在创建独立测速任务……";
        }
        try {
          const response = await postPlatform("/network/iperf3", request, { timeoutMs: 10000 });
          activeIperfTaskId = response.taskId || "";
          if (!activeIperfTaskId) throw new Error("后端没有返回任务编号");
          window.sessionStorage.setItem(iperfTaskStorageKey, activeIperfTaskId);
          if (result) result.textContent = `任务 ${activeIperfTaskId} 已开始，正在寻找可用端口……`;
          startIperfProgress();
          await refreshIperfProgress();
        } catch (error) {
          const runningTaskId = error && error.payload && error.payload.taskId;
          if (error.status === 409 && runningTaskId) {
            activeIperfTaskId = runningTaskId;
            window.sessionStorage.setItem(iperfTaskStorageKey, activeIperfTaskId);
            if (result) result.textContent = `任务 ${activeIperfTaskId} 正在运行，已连接到该任务。`;
            startIperfProgress();
            await refreshIperfProgress();
            return;
          }
          if (result) {
            result.className = "network-tool-result bad";
            result.textContent = `测速失败：${error.message}`;
          }
          iperfBtn.disabled = false;
          if (iperfStopBtn) iperfStopBtn.hidden = true;
        }
      };

      if (iperfPreset) iperfPreset.addEventListener("change", applyIperfPreset);
      if (iperfPublicServer) iperfPublicServer.addEventListener("change", applyIperfPublicServer);
      applyIperfPreset();
      loadServerConfig(fetch)
        .then((payload) => {
          iperfPresets = payload.presets;
          applyIperfPreset();
          if (iperfHint && payload.verifiedAt) {
            iperfHint.textContent += ` · 节点核验 ${payload.verifiedAt}`;
          }
        })
        .catch((error) => {
          if (iperfHint) iperfHint.textContent = `公共节点配置加载失败：${error.message}；仍可使用自定义节点。`;
        });

      if (iperfBtn) {
        iperfBtn.addEventListener("click", () => {
          const result = document.getElementById("iperfResult");
          const direction = document.getElementById("iperfDirection").value;
          const seconds = Number(document.getElementById("iperfDuration").value || 10);
          const server = document.getElementById("iperfServer").value.trim();
          if (!server) {
            if (result) {
              result.hidden = false;
              result.className = "network-tool-result bad";
              result.textContent = "请先填写自定义 iPerf3 服务器。";
            }
            return;
          }
          pendingIperfRequest = {
            server,
            ports: document.getElementById("iperfPorts").value.trim(),
            duration: document.getElementById("iperfDuration").value.trim(),
            parallel: document.getElementById("iperfParallel").value.trim(),
            direction
          };
          const estimated = seconds * (direction === "both" ? 2 : 1);
          if (iperfConfirmSummary) {
            iperfConfirmSummary.textContent = `${server} · 正常约 ${estimated} 秒，节点忙时最长约 60 秒 · 期间会主动占用公网带宽`;
          }
          if (iperfConfirm) iperfConfirm.hidden = false;
          if (iperfConfirmBtn) iperfConfirmBtn.focus();
        });
      }
      if (iperfCancelBtn) iperfCancelBtn.addEventListener("click", hideIperfConfirmation);
      if (iperfConfirmBtn) {
        iperfConfirmBtn.addEventListener("click", () => {
          if (pendingIperfRequest) executeIperfTest(pendingIperfRequest);
        });
      }
      if (iperfStopBtn) {
        iperfStopBtn.addEventListener("click", async () => {
          if (!activeIperfTaskId) return;
          iperfStopBtn.disabled = true;
          try {
            await postPlatform("/network/iperf3/stop", { taskId: activeIperfTaskId }, { timeoutMs: 5000 });
            if (iperfProgressDetail) iperfProgressDetail.textContent = "正在停止测速进程……";
          } catch (error) {
            if (iperfProgressDetail) iperfProgressDetail.textContent = `停止失败：${error.message}`;
          } finally {
            iperfStopBtn.disabled = false;
          }
        });
      }
      if (iperfHistory) {
        iperfHistory.addEventListener("click", async (event) => {
          const button = event.target.closest("button[data-task-id]");
          if (!button) return;
          try {
            renderIperfTaskResult(await fetchIperfStatus(button.dataset.taskId));
          } catch (error) {
            renderIperfTaskResult({ state: "failed", message: error.message });
          }
        });
        refreshIperfHistory();
      }
      const rememberedIperfTaskId = window.sessionStorage.getItem(iperfTaskStorageKey) || "";
      if (rememberedIperfTaskId) {
        activeIperfTaskId = rememberedIperfTaskId;
        iperfBtn.disabled = true;
        startIperfProgress();
        refreshIperfProgress();
      }
    }

    return { ensureMounted };
  }

  const ns = { createIperfController };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSIperfController = ns;
  }
}());
