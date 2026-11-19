;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const { queryError } = common ? require("./data.js") : window.BSPingData;
  const refreshSeconds = 30;
  const viewportLimit = 48;
  const concurrency = 2;
  const cacheLimit = 384;

  function createTrendLoader({ raw, changed }) {
    const cache = new Map();
    const pending = new Set();
    let wanted = new Map();
    let end = null;

    const signature = (target) => target?.dns
      ? [target.key, target.ip, target.resolutionGeneration, target.sampleNotBefore].join("|") : target?.key;

    function get(key) {
      const id = JSON.stringify([end, signature(wanted.get(key))]);
      const entry = cache.get(id);
      if (entry) {
        cache.delete(id);
        cache.set(id, entry);
      }
      return entry;
    }

    async function load(request, targets) {
      const { controller, end, keys, versions } = request;
      try {
        const rows = await raw(targets, end - model.defaultStats, end, controller.signal);
        publish((key) => ({ rows: rows.get(key) || [] }));
      } catch (error) {
        publish(() => ({ error: queryError(error) }));
      } finally {
        pending.delete(request);
        pump();
      }

      function publish(result) {
        if (controller.signal.aborted) return;
        for (const key of keys) {
          if (signature(wanted.get(key)) !== versions.get(key)) continue;
          const entry = { end, ...result(key) };
          cache.set(JSON.stringify([end, versions.get(key)]), entry);
          if (cache.size > cacheLimit) cache.delete(cache.keys().next().value);
          changed(key, entry);
        }
      }
    }

    function pump() {
      if (pending.size >= concurrency) return;
      const covered = new Set();
      for (const request of pending) {
        if (!request.controller.signal.aborted) request.keys.forEach((key) => {
          if (signature(wanted.get(key)) === request.versions.get(key)) covered.add(key);
        });
      }
      const queued = [...wanted.values()].filter(({ key }) => !covered.has(key) && !get(key));
      while (pending.size < concurrency && queued.length) {
        const targets = queued.splice(0, model.maxRawTargets);
        const versions = new Map(targets.map((target) => [target.key, signature(target)]));
        const request = { end, keys: [...versions.keys()], versions, controller: new AbortController() };
        pending.add(request);
        void load(request, targets);
      }
    }

    function update(targets, windowEnd) {
      wanted = new Map(targets.slice(0, viewportLimit).map((target) => [target.key, target]));
      end = windowEnd;
      for (const request of pending) {
        if (request.end !== end || request.keys.every((key) =>
          signature(wanted.get(key)) !== request.versions.get(key))) request.controller.abort();
      }
      pump();
    }

    function stop() {
      update([], null);
      cache.clear();
    }

    return { update, get, stop };
  }

  const api = { createTrendLoader, viewportLimit, refreshSeconds };
  if (common) module.exports = api;
  else window.BSPingTrends = api;
})();
