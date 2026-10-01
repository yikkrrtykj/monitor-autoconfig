;(function () {
  'use strict';
  const MAX_APS = 512;
  function mac(value) {
    const normalized = String(value || '').toLowerCase().replace(/[:.\-]/g, '');
    return /^[0-9a-f]{12}$/.test(normalized) && normalized !== '000000000000' && normalized !== 'ffffffffffff' ? normalized : '';
  }
  function ipv4(value) {
    return typeof value === 'string' && /^(\d{1,3}\.){3}\d{1,3}$/.test(value) && value.split('.').every((v) => Number(v) <= 255 && String(Number(v)) === v);
  }
  function identities(rows) {
    if (!Array.isArray(rows) || rows.length > MAX_APS) return [];
    const parsed = rows.map((ap) => ap && typeof ap === 'object' ? ({ ...ap, mac: mac(ap.mac) }) : {});
    return parsed.filter((ap) => ipv4(ap.ip) && ap.mac && parsed.filter((other) => other.ip === ap.ip).length === 1
      && parsed.filter((other) => other.mac === ap.mac).length === 1);
  }
  function appendApLeaves(frame, data, now = Date.now() / 1000) {
    const aps = identities(data && data.aps);
    const artifact = data && data.artifact;
    const result = { ...frame, layout: { ...frame.layout, nodes: [...frame.layout.nodes], links: [...frame.layout.links] }, apCount: { located: 0, total: aps.length } };
    if (!aps.length || !artifact || artifact.source !== 'librenms-fdb+snmp-exact' || artifact.candidate_source !== 'librenms-fdb' || !Array.isArray(artifact.attachments)
      || artifact.attachments.length > MAX_APS || !Number.isFinite(artifact.generated_at)
      || !Number.isFinite(artifact.max_age_seconds) || artifact.max_age_seconds < 0) return result;
    const elapsed = now - artifact.generated_at;
    // A stopped collector must not keep AP edges alive indefinitely.
    if (elapsed < 0 || elapsed > artifact.max_age_seconds) return result;
    const resolved = [];
    aps.forEach((ap) => {
      const matches = artifact.attachments.filter((row) => row && typeof row === 'object' && row.ap_ip === ap.ip && mac(row.ap_mac) === ap.mac);
      if (matches.length !== 1) return;
      const row = matches[0];
      if (!Number.isFinite(row.evidence_age_seconds) || row.evidence_age_seconds < 0
        || row.evidence_age_seconds + elapsed > artifact.max_age_seconds || !Number.isInteger(row.switch_ifindex) || row.switch_ifindex <= 0) return;
      const parents = frame.layout.nodes.filter((node) => node.ip === row.switch_ip && node.kind === 'dist');
      if (parents.length === 1) resolved.push({ ap, row, parent: parents[0] });
    });
    // Ordering affects positions only, never attachment authority.
    resolved.sort((a, b) => a.parent.x - b.parent.x || a.ap.ip.localeCompare(b.ap.ip));
    const y = Math.max(0, ...frame.layout.nodes.map((node) => node.y + node.h)) + 90;
    let right = 24;
    resolved.forEach(({ ap, row, parent }) => {
      const node = { kind: 'ap', ip: ap.ip, name: ap.name || ap.ip, model: ap.model, clients: ap.clients,
        parentIp: row.switch_ip, parentIfindex: row.switch_ifindex, switchPort: row.switch_port,
        success: ap.online, level: ap.online === true ? 'good' : ap.online === false ? 'bad' : 'none',
        x: Math.max(right, parent.x), y, w: 192, h: 74 };
      right = node.x + node.w + 24;
      result.layout.nodes.push(node);
      result.layout.links.push({ from: parent, to: node, severity: node.level, fallback: false, label: row.switch_port });
    });
    result.width = Math.max(frame.width, right);
    result.layout.height = Math.max(frame.layout.height, resolved.length ? y + 98 : 0);
    result.apCount.located = resolved.length;
    return result;
  }
  const ns = { appendApLeaves };
  if (typeof module !== 'undefined' && module.exports) module.exports = ns;
  else window.BSApTopology = ns;
}());
