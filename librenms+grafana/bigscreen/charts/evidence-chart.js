;(function () {
  'use strict';

  function createEvidenceChartRenderer(dependencies) {
    const {
      document,
      renderLineChart,
      formatPingText,
      estimateStepSeconds,
      average,
      escapeHtml
    } = dependencies;

    function formatOnlineAxis(value) {
      if (value <= 0.01) return "离线";
      if (value >= 0.99) return "在线";
      return "";
    }

    function formatOnlineState(value) {
      return value >= 0.5 ? "在线" : "离线";
    }

    function flattenSeriesValues(seriesList) {
      return seriesList.flatMap((series) => series.values.map((point) => point.v)).filter((value) => Number.isFinite(value));
    }

    function evidenceVerdict(latencyValues, successValues) {
      const maxLatency = latencyValues.length ? Math.max(...latencyValues) : null;
      const avgLatency = latencyValues.length ? average(latencyValues) : null;
      const failCount = successValues.filter((value) => value < 0.5).length;

      if (!latencyValues.length && !successValues.length) {
        return { level: "unknown", text: "没有查到数据" };
      }
      if (failCount > 0) {
        return { level: "bad", text: "存在断线/探测失败" };
      }
      if (avgLatency !== null && avgLatency >= 0.08) {
        return { level: "bad", text: "持续高延迟" };
      }
      if (maxLatency !== null && maxLatency >= 0.1) {
        return { level: "warn", text: "有高延迟尖峰" };
      }
      if (maxLatency !== null && maxLatency >= 0.04) {
        return { level: "warn", text: "有轻微抖动" };
      }
      return { level: "good", text: "未见明显网络异常" };
    }

    function renderEvidenceSummary(containerId, context, latencySeries, successSeries) {
      const container = document.getElementById(containerId);
      const latencyValues = flattenSeriesValues(latencySeries);
      const successValues = flattenSeriesValues(successSeries);
      const verdict = evidenceVerdict(latencyValues, successValues);
      const maxLatency = latencyValues.length ? formatPingText(Math.max(...latencyValues)) : "-";
      const avgLatency = latencyValues.length ? formatPingText(average(latencyValues)) : "-";
      const onlineRate = successValues.length ? `${(average(successValues) * 100).toFixed(1)}%` : "-";
      const failCount = successValues.filter((value) => value < 0.5).length;
      const offlineSeconds = failCount ? `${Math.round(failCount * estimateStepSeconds(successSeries))}s` : "0s";

      container.innerHTML = `
        <div class="evidence-verdict ${verdict.level}">
          <span>${escapeHtml(context.label)}</span>
          <strong>${escapeHtml(verdict.text)}</strong>
        </div>
        <div class="evidence-kpis">
          <div><span>平均延迟</span><strong>${escapeHtml(avgLatency)}</strong></div>
          <div><span>最高延迟</span><strong>${escapeHtml(maxLatency)}</strong></div>
          <div><span>在线率</span><strong>${escapeHtml(onlineRate)}</strong></div>
          <div><span>离线累计</span><strong>${escapeHtml(offlineSeconds)}</strong></div>
        </div>
      `;
    }

    return function renderEvidenceCharts(input) {
      const {
        summaryContainerId,
        latencyContainerId,
        successContainerId,
        context,
        latencySeries,
        successSeries
      } = input;
      renderEvidenceSummary(summaryContainerId, context, latencySeries, successSeries);
      const latencyGap = Math.max(5, estimateStepSeconds(latencySeries) * 3);
      const successGap = Math.max(5, estimateStepSeconds(successSeries) * 3);
      renderLineChart(latencyContainerId, latencySeries, {
        axisFormatter: formatPingText,
        valueFormatter: formatPingText,
        minMax: 0.005,
        smooth: true,
        breakGapSeconds: latencyGap,
        legend: "bottom"
      });
      renderLineChart(successContainerId, successSeries.map((series) => ({ ...series, color: "#73d17a" })), {
        axisFormatter: formatOnlineAxis,
        valueFormatter: formatOnlineState,
        calcs: ["last", "min"],
        minMax: 1,
        smooth: false,
        step: true,
        breakGapSeconds: successGap,
        fill: true,
        legend: "bottom"
      });
    };
  }

  const ns = { createEvidenceChartRenderer };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSEvidenceChart = ns;
  }
}());
