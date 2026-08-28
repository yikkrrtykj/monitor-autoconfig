;(function () {
  'use strict';

  function createIncidentRegistry(dependencies) {
    const {
      document,
      escapeHtml,
      formatTimestampFull,
      fetchIncidents,
      postPlatform,
      patchPlatform,
      getControlReport,
      now
    } = dependencies;

    let lastIncidents = [];

    function render(payload) {
      const incidents = payload && payload.incidents ? payload.incidents : [];
      lastIncidents = incidents;
      const list = document.getElementById("controlIncidentList");
      if (!list) return;
      if (payload && payload.error) {
        list.innerHTML = `<div class="control-empty bad">${escapeHtml(payload.error)}</div>`;
        return;
      }
      if (!incidents.length) {
        list.innerHTML = `<div class="control-empty">暂无事故记录</div>`;
        return;
      }
      list.innerHTML = incidents.slice(0, 12).map((item) => {
        const started = item.startedAt ? formatTimestampFull(item.startedAt) : "-";
        const duration = item.recoveredAt && item.startedAt ? `${Math.max(0, Math.round((item.recoveredAt - item.startedAt) / 60))} 分钟` : "进行中";
        return `
          <div class="incident-record ${item.severity || "warn"}">
            <span>#${escapeHtml(item.id)} · ${escapeHtml(item.status || "open")}</span>
            <strong>${escapeHtml(item.title || "")}</strong>
            <em>${escapeHtml(started)} · ${escapeHtml(duration)} · ${escapeHtml(item.owner || "未分配")}</em>
            ${item.status === "resolved" ? "" : `<button type="button" data-resolve-incident="${escapeHtml(item.id)}">标记恢复</button>`}
          </div>
        `;
      }).join("");
      list.querySelectorAll("[data-resolve-incident]").forEach((button) => {
        button.addEventListener("click", async () => {
          try {
            await patchPlatform(`/incidents/${button.dataset.resolveIncident}`, {
              status: "resolved",
              recoveredAt: Math.floor(now() / 1000),
              event: "标记恢复",
              eventType: "recovery"
            });
            render(await fetchIncidents());
          } catch (error) {
            render({ incidents: lastIncidents, error: error.message || "更新事故失败" });
          }
        });
      });
    }

    async function createIncident() {
      const input = document.getElementById("controlIncidentTitle");
      const title = (input && input.value.trim()) || "现场事故";
      const controlReport = getControlReport();
      const related = controlReport ? {
        readiness: controlReport.readiness,
        checks: controlReport.checks.filter((item) => item.level === "bad" || item.level === "warn").slice(0, 8)
      } : {};
      try {
        await postPlatform("/incidents", { title, severity: controlReport && controlReport.readiness.level === "bad" ? "bad" : "warn", related });
        if (input) input.value = "";
        render(await fetchIncidents());
      } catch (error) {
        render({ incidents: lastIncidents, error: error.message || "创建事故失败" });
      }
    }

    function bind() {
      const incidentCreate = document.getElementById("controlIncidentCreate");
      if (incidentCreate && !incidentCreate.dataset.bound) {
        incidentCreate.addEventListener("click", createIncident);
        incidentCreate.dataset.bound = "1";
      }
    }

    return { bind, render };
  }

  const ns = { createIncidentRegistry };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSIncidentRegistry = ns;
  }
}());
