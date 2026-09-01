;(function () {
  'use strict';

  function createDeliveryPanel(dependencies) {
    const {
      document,
      setTimeout,
      escapeHtml,
      postPlatform,
      fetchRetirePending,
      iperfController
    } = dependencies;

    function render() {
      const element = document.getElementById("controlDelivery");
      if (!element) return;
      // Render once so periodic status refreshes do not wipe manually entered
      // diagnostic settings or the result the operator is reading.
      if (element.dataset.built === "1") return;
      element.dataset.built = "1";
      element.innerHTML = `
        <div class="delivery-actions">
          <button type="button" class="delivery-test-alert" id="preCheckBtn">赛前体检</button>
          <button type="button" class="delivery-test-alert" id="testAlertBtn">发送测试告警</button>
          <span class="test-alert-result" id="testAlertResult"></span>
        </div>
        <div class="precheck-result" id="preCheckResult" hidden></div>
      `;
      iperfController.ensureMounted(element);
      const preBtn = document.getElementById("preCheckBtn");
      if (preBtn) {
        preBtn.addEventListener("click", async () => {
          const box = document.getElementById("preCheckResult");
          preBtn.disabled = true;
          if (box) { box.hidden = false; box.className = "precheck-result"; box.textContent = "体检中…（最长约 2 分钟）"; }
          try {
            const res = await postPlatform("/pre-check", {});
            if (box) {
              if (!res || !res.ok) {
                box.className = "precheck-result bad";
                box.textContent = `体检失败：${(res && res.error) || "未知错误"}`;
              } else {
                const verdictText = { good: "✅ 可以开赛", warn: "⚠ 有警告，请确认", bad: "❌ 需要处理" }[res.verdict] || res.verdict;
                box.className = `precheck-result ${res.verdict}`;
                box.innerHTML = `<div class="precheck-verdict">${verdictText}　通过 ${res.pass} · 警告 ${res.warn} · 失败 ${res.fail}</div><pre>${escapeHtml(res.output || "")}</pre>`;
              }
            }
          } catch (error) {
            if (box) { box.className = "precheck-result bad"; box.textContent = `体检失败：${error.message}`; }
          } finally {
            preBtn.disabled = false;
          }
        });
      }
      const testBtn = document.getElementById("testAlertBtn");
      if (testBtn) {
        testBtn.addEventListener("click", async () => {
          const result = document.getElementById("testAlertResult");
          testBtn.disabled = true;
          if (result) { result.textContent = "发送中…"; result.className = "test-alert-result"; }
          try {
            const res = await postPlatform("/test-alert", {});
            const ok = Boolean(res && res.ok);
            if (result) {
              const channel = { app: "自建应用", webhook: "群机器人 Webhook", "dry-run": "DryRun" }[res && res.channel] || "未知通道";
              const fellBack = ok && res && res.channel === "webhook" && res.appError;
              result.textContent = ok
                ? (res.dryRun
                  ? "已触发（DryRun 模式，未真正发送）"
                  : fellBack
                    ? `已通过 Webhook 回退发送；自建应用失败：${res.appError}`
                    : `已通过${channel}发送，请到飞书群确认收到`)
                : `失败：${(res && (res.appError || res.error)) || "未知错误"}`;
              result.className = `test-alert-result ${fellBack ? "warn" : ok ? "good" : "bad"}`;
            }
          } catch (error) {
            if (result) { result.textContent = `失败：${error.message}`; result.className = "test-alert-result bad"; }
          } finally {
            testBtn.disabled = false;
          }
        });
      }

      const mountRetirePending = async () => {
        let initial;
        try {
          initial = await fetchRetirePending();
        } catch (_error) {
          return;
        }
        if (!initial || initial.enabled !== true) return;
        element.insertAdjacentHTML("beforeend", `
          <section class="network-tool" aria-labelledby="retirePendingTitle">
            <div class="network-tool-heading">
              <div>
                <h3 id="retirePendingTitle">待删除设备</h3>
                <p>离线满 48 小时的设备在这里等人工确认；不确认永远不会自动删除。飞书确认卡与此面板等效。</p>
              </div>
              <button type="button" class="delivery-test-alert" id="retirePendingRefreshBtn">刷新列表</button>
            </div>
            <div class="network-tool-result" id="retirePendingList" hidden></div>
          </section>
        `);
        const retireList = document.getElementById("retirePendingList");
        const retireRefreshBtn = document.getElementById("retirePendingRefreshBtn");

        const renderRetirePending = (payload) => {
          if (!retireList) return;
          retireList.hidden = false;
          const pending = (payload && payload.pending) || [];
          if (payload && payload.error) {
            retireList.className = "network-tool-result bad";
            retireList.textContent = payload.error;
            return;
          }
          if (!pending.length) {
            retireList.className = "network-tool-result good";
            retireList.textContent = "没有待删除设备。";
            return;
          }
          retireList.className = "network-tool-result warn";
          retireList.innerHTML = pending.map((item) => {
            const name = escapeHtml(item.name || item.ip || "?");
            const ip = escapeHtml(item.ip || "");
            const downSince = item.downSince
              ? new Date(item.downSince * 1000).toLocaleString("zh-CN", { hour12: false })
              : "未知";
            return `
              <div class="retire-pending-row" data-key="${escapeHtml(item.key)}" data-token="${escapeHtml(item.token)}">
                <span>${name}${ip && ip !== name ? ` (${ip})` : ""} · 离线自 ${escapeHtml(downSince)}</span>
                <button type="button" class="delivery-test-alert" data-retire-action="delete">确认删除</button>
                <button type="button" class="delivery-test-alert" data-retire-action="keep">保留设备</button>
              </div>`;
          }).join("");
        };

        const refreshRetirePending = async () => {
          renderRetirePending(await fetchRetirePending());
        };

        if (retireRefreshBtn) retireRefreshBtn.addEventListener("click", refreshRetirePending);
        if (retireList) {
          retireList.addEventListener("click", async (event) => {
            const button = event.target.closest("button[data-retire-action]");
            if (!button) return;
            const row = button.closest(".retire-pending-row");
            if (!row) return;
            const action = button.dataset.retireAction;
            if (action === "delete" && button.dataset.armed !== "1") {
              button.dataset.armed = "1";
              button.textContent = "再点一次确认删除";
              setTimeout(() => {
                button.dataset.armed = "";
                button.textContent = "确认删除";
              }, 5000);
              return;
            }
            button.disabled = true;
            try {
              const result = await postPlatform("/network/retire/resolve", {
                key: row.dataset.key,
                token: row.dataset.token,
                action,
              });
              if (!result || result.ok !== true) {
                renderRetirePending({ error: (result && result.error) || "操作失败" });
                setTimeout(refreshRetirePending, 1500);
                return;
              }
              await refreshRetirePending();
            } catch (error) {
              renderRetirePending({ error: `操作失败：${error.message}` });
            } finally {
              button.disabled = false;
            }
          });
          renderRetirePending(initial);
        }
      };
      mountRetirePending();
    }

    return { render };
  }

  const ns = { createDeliveryPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSDeliveryPanel = ns;
  }
}());
