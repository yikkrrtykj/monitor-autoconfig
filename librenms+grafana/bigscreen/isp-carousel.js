;(function (root, factory) {
  const ns = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = ns;
  } else {
    root.BSIspCarousel = ns;
  }
}(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function pageCount(itemCount, pageSize = 2) {
    const size = Math.max(1, Number(pageSize) || 1);
    return Math.ceil(Math.max(0, Number(itemCount) || 0) / size);
  }

  function clampPageIndex(pageIndex, itemCount, pageSize = 2) {
    const pages = pageCount(itemCount, pageSize);
    if (!pages) return 0;
    return Math.min(pages - 1, Math.max(0, Math.trunc(Number(pageIndex) || 0)));
  }

  function createIspCarousel(options = {}) {
    const pageSize = Math.max(1, Number(options.pageSize) || 2);
    const intervalMs = Math.max(1, Number(options.intervalMs) || 10000);
    const setIntervalFn = options.setIntervalFn || ((callback, delay) => setInterval(callback, delay));
    const clearIntervalFn = options.clearIntervalFn || ((handle) => clearInterval(handle));
    const onPageChange = typeof options.onPageChange === "function" ? options.onPageChange : () => {};

    let itemCount = 0;
    let pageIndex = 0;
    let active = false;
    let timer = null;

    function snapshot() {
      const pages = pageCount(itemCount, pageSize);
      const start = pageIndex * pageSize;
      return {
        active,
        itemCount,
        pageSize,
        pageIndex,
        pageNumber: pages ? pageIndex + 1 : 0,
        pageCount: pages,
        start,
        end: Math.min(itemCount, start + pageSize),
        canPrevious: pageIndex > 0,
        canNext: pageIndex + 1 < pages,
        timerRunning: timer !== null
      };
    }

    function clearTimer() {
      if (timer === null) return;
      clearIntervalFn(timer);
      timer = null;
    }

    function autoAdvance() {
      const pages = pageCount(itemCount, pageSize);
      if (!active || pages <= 1) return;
      pageIndex = (pageIndex + 1) % pages;
      onPageChange(snapshot());
    }

    function syncTimer(restart = false) {
      const shouldRun = active && pageCount(itemCount, pageSize) > 1;
      if (!shouldRun) {
        clearTimer();
        return;
      }
      if (restart) clearTimer();
      if (timer === null) timer = setIntervalFn(autoAdvance, intervalMs);
    }

    function updateTotal(nextItemCount) {
      itemCount = Math.max(0, Math.trunc(Number(nextItemCount) || 0));
      pageIndex = clampPageIndex(pageIndex, itemCount, pageSize);
      // A five-second data refresh reaches this method too. Keep an existing
      // timer intact so data updates never restart the ten-second page clock.
      syncTimer(false);
      return snapshot();
    }

    function activate({ reset = true } = {}) {
      active = true;
      if (reset) pageIndex = 0;
      pageIndex = clampPageIndex(pageIndex, itemCount, pageSize);
      syncTimer(reset);
      return snapshot();
    }

    function deactivate({ reset = true } = {}) {
      active = false;
      clearTimer();
      if (reset) pageIndex = 0;
      return snapshot();
    }

    function move(delta) {
      const nextIndex = clampPageIndex(pageIndex + Number(delta || 0), itemCount, pageSize);
      if (nextIndex !== pageIndex) {
        pageIndex = nextIndex;
        onPageChange(snapshot());
      }
      // Manual navigation starts a fresh ten-second interval, including when
      // callers request a boundary page that is already selected.
      syncTimer(true);
      return snapshot();
    }

    function visibleItems(items) {
      const list = Array.isArray(items) ? items : [];
      const state = snapshot();
      return list.slice(state.start, state.end);
    }

    return {
      updateTotal,
      activate,
      deactivate,
      move,
      visibleItems,
      snapshot
    };
  }

  return { pageCount, clampPageIndex, createIspCarousel };
}));
