;(function () {
  'use strict';

  const base = "/platform-api/ping/targets";

  function failure(code, status, detail) {
    return Object.assign(new Error(code), { code, status, detail });
  }

  function uuid(crypto) {
    if (!crypto?.getRandomValues) throw failure("random_unavailable");
    if (typeof crypto.randomUUID === "function") return crypto.randomUUID().toLowerCase();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = bytes[6] & 15 | 64;
    bytes[8] = bytes[8] & 63 | 128;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join("-");
  }

  function createClient({ fetch, crypto, timeout = 10000 }) {
    async function request(path, options = {}, signal) {
      const controller = new AbortController();
      const abort = () => controller.abort();
      if (signal?.aborted) abort();
      else signal?.addEventListener("abort", abort, { once: true });
      const timer = setTimeout(abort, timeout);
      try {
        const response = await fetch(path, {
          ...options, credentials: "same-origin", cache: "no-store", signal: controller.signal
        });
        const result = await response.json().catch(() => null);
        if (!response.ok) throw failure(result?.error || "http", response.status, result);
        if (result?.ok !== true) throw failure("invalid_response");
        return result;
      } catch (error) {
        if (signal?.aborted) throw failure("cancelled");
        if (controller.signal.aborted) throw failure("timeout");
        throw error.code ? error : failure("network");
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
      }
    }

    function operation(method, target, payload) {
      return Object.freeze({
        method, key: uuid(crypto),
        path: target ? base + "/" + encodeURIComponent(target.id) : base,
        version: target ? JSON.stringify(String(target.revision)) : null,
        body: payload === undefined ? undefined : JSON.stringify(payload)
      });
    }

    function write(op, token, signal) {
      const headers = { "X-CSRF-Token": token, "Idempotency-Key": op.key };
      if (op.version) headers["If-Match"] = op.version;
      if (op.body !== undefined) headers["Content-Type"] = "application/json";
      return request(op.path, { method: op.method, headers, body: op.body }, signal);
    }

    return { list: (signal) => request(base, {}, signal),
      discovery: (signal) => request(base + "/discovery", {}, signal),
      csrf: (signal) => request(base + "/csrf", {}, signal), operation, write };
  }

  const api = { createClient };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else window.BSPingCustomAPI = api;
})();
