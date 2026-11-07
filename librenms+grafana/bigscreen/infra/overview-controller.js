;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./overview-model.js") : window.BSOverviewModel;

  function createOverviewController({ data, render, clock = () => Math.floor(Date.now() / 1000),
    schedule = setInterval, cancel = clearInterval }) {
    let active = null;
    let timer = null;
    let domains;
    let lastProbe;

    function draw() {
      render(model.overviewState(domains, clock()), {
        lastProbe, pingState: domains.pingState.state, wanState: domains.wanState.state
      });
    }

    async function poll(domain, interval, at, read, apply) {
      if (domain.busy || at < domain.due) return;
      const request = active;
      domain.busy = true;
      domain.due = at + interval;
      try {
        const result = await read(request.signal);
        if (active !== request) return;
        apply(result);
      } catch {
        if (active !== request) return;
        domain.state = "error";
      } finally {
        if (active === request) {
          domain.busy = false;
          draw();
        }
      }
    }

    function tick() {
      const at = clock();
      const { pingState, wanState, statsState } = domains;
      draw();
      poll(pingState, 5, at, (signal) => data.summary(at, signal), (result) => {
        pingState.data = result;
        pingState.state = "ready";
        for (const { sourceAt } of result.targets) {
          if (sourceAt != null) lastProbe = Math.max(lastProbe ?? 0, sourceAt);
        }
        const gateways = model.gatewayTargets(result.targets).filter(({ status }) => status !== "UNKNOWN");
        if (gateways.length) {
          poll(statsState, 30, at, (signal) => data.gatewayStats(gateways, at, signal), (stats) => {
            statsState.data = stats;
            statsState.at = at;
            statsState.state = "ready";
          });
        }
      });
      poll(wanState, 15, at, (signal) => data.bandwidth(at, signal), ({ cards, failed }) => {
        wanState.data = cards;
        wanState.state = failed ? "error" : "ready";
      });
    }

    function stop() {
      if (!active) return;
      active.abort();
      active = null;
      cancel(timer);
    }

    function start() {
      stop();
      active = new AbortController();
      const initial = () => ({ state: "loading", data: null, at: null, due: 0, busy: false });
      domains = { pingState: initial(), wanState: initial(), statsState: initial() };
      lastProbe = null;
      tick();
      timer = schedule(tick, 5000);
    }

    return { start, stop };
  }

  if (common) module.exports = { createOverviewController };
  else window.BSOverviewController = { createOverviewController };
})();
