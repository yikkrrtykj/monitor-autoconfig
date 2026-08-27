;(function () {
  'use strict';

  // Keep the legacy range, compact-list and standalone conversion paths
  // separate: their input gates and coercion contracts are not identical.
  function rangeIpv4Number(value) {
    const parts = value.split(".").map(Number);
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
    return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256) + parts[3];
  }

  function compactIpv4Number(value) {
    const parts = String(value || "").split(".").map(Number);
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
    return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256) + parts[3];
  }

  function dhcpRangeAddresses(rangeText, limit = 4096) {
    const match = String(rangeText || "").match(/^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$/);
    if (!match) return [];
    const toAddress = (value) => [24, 16, 8, 0].map((shift) => Math.floor(value / (2 ** shift)) % 256).join(".");
    const start = rangeIpv4Number(match[1]);
    const end = rangeIpv4Number(match[2]);
    if (start == null || end == null || end < start || end - start + 1 > limit) return [];
    return Array.from({ length: end - start + 1 }, (_item, index) => toAddress(start + index));
  }

  function compactDhcpAddresses(values) {
    const entries = [...new Set(values || [])]
      .map((ip) => ({ ip, number: compactIpv4Number(ip) }))
      .filter((item) => item.number != null)
      .sort((left, right) => left.number - right.number);
    const ranges = [];
    for (const entry of entries) {
      const current = ranges[ranges.length - 1];
      if (current && entry.number === current.endNumber + 1) {
        current.end = entry.ip;
        current.endNumber = entry.number;
      } else {
        ranges.push({ start: entry.ip, end: entry.ip, endNumber: entry.number });
      }
    }
    return ranges.map((range) => range.start === range.end ? range.start : `${range.start}–${range.end}`).join("、");
  }

  function dhcpPoolKey(pool) {
    return `${encodeURIComponent(String(pool.name || ""))}|${encodeURIComponent(String(pool.range || ""))}`;
  }

  function dhcpIpv4Number(value) {
    const parts = String(value || "").trim().split(".").map(Number);
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
    return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256) + parts[3];
  }

  function dhcpPoolMatchesSearch(pool, query) {
    const needle = String(query || "").trim().toLowerCase();
    if (!needle) return true;
    const searchable = `${pool.name || ""} ${pool.range || ""}`.toLowerCase();
    if (searchable.includes(needle)) return true;
    const address = dhcpIpv4Number(needle);
    const rangeMatch = String(pool.range || "").match(/^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$/);
    if (address == null || !rangeMatch) return false;
    const start = dhcpIpv4Number(rangeMatch[1]);
    const end = dhcpIpv4Number(rangeMatch[2]);
    return start != null && end != null && address >= start && address <= end;
  }

  function dhcpPoolMatchesFilter(pool, conflicts, filterValue) {
    if (filterValue === "active") return Number(pool.leased || 0) > 0;
    if (filterValue === "excluded") return Number(pool.excluded || 0) > 0;
    if (filterValue === "attention") {
      const poolAddresses = new Set(dhcpRangeAddresses(pool.range));
      return ["warn", "bad"].includes(String(pool.level || ""))
        || (conflicts || []).some((ip) => poolAddresses.has(ip));
    }
    return true;
  }

  function dhcpPoolSortValue(pool) {
    const match = String(pool.range || "").match(/^\s*(\d{1,3}(?:\.\d{1,3}){3})/);
    return match ? dhcpIpv4Number(match[1]) : null;
  }

  function compareDhcpPools(left, right) {
    const leftValue = dhcpPoolSortValue(left);
    const rightValue = dhcpPoolSortValue(right);
    if (leftValue != null && rightValue != null && leftValue !== rightValue) return leftValue - rightValue;
    if (leftValue != null && rightValue == null) return -1;
    if (leftValue == null && rightValue != null) return 1;
    return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN", { numeric: true });
  }

  function buildDhcpAddressContext(pool, conflicts, bindingPayload) {
    const excluded = new Set(pool.excludedAddresses || []);
    const conflictSet = new Set(conflicts || []);
    const bindingDetails = new Map((bindingPayload && bindingPayload.bindings || [])
      .map((item) => [String(item.ip || ""), String(item.detail || "")]));
    const arpDetails = new Map((bindingPayload && bindingPayload.arpEntries || [])
      .map((item) => [String(item.ip || ""), String(item.detail || "")]));
    const used = new Set(bindingPayload && bindingPayload.usedAddresses || []);
    const observed = new Set(bindingPayload && bindingPayload.observedAddresses || []);
    const reservedUsed = new Set([...excluded].filter((ip) => used.has(ip) || observed.has(ip)));
    return { excluded, conflictSet, bindingDetails, arpDetails, used, observed, reservedUsed };
  }

  function dhcpAddressState(ip, conflictSet, reservedUsed, excluded, used) {
    return conflictSet.has(ip) ? "conflict"
      : reservedUsed.has(ip) ? "reserved-used"
      : excluded.has(ip) ? "excluded"
      : used.has(ip) ? "used"
      : "pool";
  }

  const ns = {
    dhcpRangeAddresses,
    compactDhcpAddresses,
    dhcpPoolKey,
    dhcpIpv4Number,
    dhcpPoolMatchesSearch,
    dhcpPoolMatchesFilter,
    dhcpPoolSortValue,
    compareDhcpPools,
    buildDhcpAddressContext,
    dhcpAddressState
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSDhcpModel = ns;
  }
}());
