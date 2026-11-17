;(function () {
  'use strict';

  const { BSPingCharts: charts, BSPingModel: model } = window;
  const { viewportLimit } = window.BSPingTrends;

  function trendLabel({ target, trend }) {
    if (trend?.error) return trend.error;
    if (target.reason) return target.reason;
    if (!trend) return "正在读取小图…";
    return trend.rows.length ? "近 5 分钟" : "近 5 分钟无样本";
  }

  function createPingWall({ root, grid, viewportChanged }) {
    const cards = new Map();
    const nearby = new Map();
    let timer;
    let observer;

    function readViewport() {
      clearTimeout(timer);
      // Coalesce observer notifications during a scroll or a filter change.
      timer = setTimeout(() => viewportChanged([...nearby.values()]
        .slice(0, viewportLimit).map(({ target }) => target)), 100);
    }

    function start() {
      observer = new IntersectionObserver((entries, source) => {
        if (observer !== source) return;
        for (const entry of entries) {
          const { key } = entry.target.dataset;
          const card = cards.get(key);
          if (card?.node !== entry.target) continue;
          if (entry.isIntersecting) nearby.set(key, card);
          else if (nearby.delete(key)) clearTrend(card);
        }
        readViewport();
      }, { root, rootMargin: "160px 0px" });
      for (const { node } of cards.values()) observer.observe(node);
    }

    function clearTrend(card) {
      card.trend = null;
      card.chart.textContent = "";
      card.note.textContent = card.target.reason || "近 5 分钟";
      card.note.removeAttribute("title");
    }

    function stop() {
      clearTimeout(timer);
      observer?.disconnect();
      observer = null;
      for (const card of nearby.values()) clearTrend(card);
      nearby.clear();
    }

    function createCard(key) {
      const node = document.createElement("article");
      node.className = "ping-card";
      node.dataset.key = key;
      node.innerHTML = `<button type="button" class="ping-card-open" data-action="open">
        <span class="ping-card-header"><strong></strong><span class="ping-badge"></span></span>
        <span class="ping-card-meta"><span class="ping-card-group"></span><span class="ping-card-ip"></span></span>
        <span class="ping-card-metrics"><span>RTT <b></b></span><span>Loss <b></b></span></span>
        <span class="ping-card-chart"></span><span class="ping-card-note">近 5 分钟</span>
      </button><button type="button" class="ping-card-compare" data-action="compare"></button>`;
      const [rtt, loss] = node.querySelectorAll("b");
      return {
        node, title: node.querySelector("strong"), badge: node.querySelector(".ping-badge"),
        group: node.querySelector(".ping-card-group"), ip: node.querySelector(".ping-card-ip"),
        rtt, loss, chart: node.querySelector(".ping-card-chart"), note: node.querySelector(".ping-card-note"),
        compare: node.querySelector(".ping-card-compare")
      };
    }

    function updateCard(card, target, selected) {
      const set = (element, text) => {
        if (element.textContent !== text) element.textContent = text;
      };
      const previous = card.target;
      const { name, ip, reason, status, rtt, loss } = target;
      const picked = selected.has(target.key);
      card.target = target;
      if (card.title.textContent !== name) card.title.title = name;
      set(card.title, name);
      set(card.group, model.groupLabel(target));
      set(card.ip, ip);
      if (card.node.dataset.state !== status) {
        card.node.dataset.state = status;
        card.badge.dataset.state = status;
      }
      set(card.badge, status);
      if (card.badge.title !== reason) card.badge.title = reason;
      if (previous?.reason !== reason || previous?.status !== status) {
        card.badge.setAttribute("aria-label", reason ? `${status} · ${reason}` : status);
        set(card.note, nearby.has(target.key) ? trendLabel(card) : reason || "近 5 分钟");
      }
      set(card.rtt, rtt === null ? "N/A" : `${charts.fmt(rtt)} ms`);
      set(card.loss, loss.loss === null ? "N/A" : `${loss.loss.toFixed(1)}%`);
      card.loss.title = `${target.lossWindow / 60} 分钟 · ${loss.failed} 失败 / ${loss.total} 有效 · 覆盖 ${(loss.coverage * 100).toFixed(1)}%`;
      set(card.compare, picked ? "✓ 已选" : "+ 对比");
      const disabled = !picked && selected.size >= model.maxCompared;
      if (card.compare.disabled !== disabled) card.compare.disabled = disabled;
      if (card.compare.getAttribute("aria-pressed") !== String(picked)) {
        card.compare.setAttribute("aria-pressed", String(picked));
        card.compare.setAttribute("aria-label", `${picked ? "移除" : "对比"} ${name}`);
      }
    }

    function render(targets, selected) {
      for (const [key, card] of cards) {
        if (targets.has(key)) continue;
        observer?.unobserve(card.node);
        nearby.delete(key);
        card.node.remove();
        cards.delete(key);
      }
      let next = grid.firstElementChild;
      for (const [key, target] of targets) {
        let card = cards.get(key);
        if (!card) {
          card = createCard(key);
          cards.set(key, card);
          observer?.observe(card.node);
        }
        updateCard(card, target, selected);
        if (card.node !== next) grid.insertBefore(card.node, next);
        next = card.node.nextElementSibling;
      }
    }

    function paint(key, trend) {
      const card = nearby.get(key);
      if (!card || card.trend === trend) return;
      if (!trend && card.trend) return; // Keep the preceding window until its successor arrives.
      card.trend = trend;
      card.chart.innerHTML = trend?.rows
        ? charts.sparkline(trend.rows, trend.end - model.defaultStats, trend.end, card.target.interval)
        : "";
      card.note.textContent = trendLabel(card);
      card.note.title = trend ? `窗口结束 ${charts.formatTime(trend.end)}` : "";
    }

    return { start, stop, render, paint };
  }

  window.BSPingWall = { createPingWall };
})();
