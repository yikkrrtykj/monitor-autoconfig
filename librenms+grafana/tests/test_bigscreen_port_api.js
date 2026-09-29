const assert = require('assert');
global.window = { BIGSCREEN_CONFIG: {}, BIGSCREEN_QUERIES: {} };
const api = require('../bigscreen/api.js');

const requests = [];
global.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, json: async () => ({ ok: true, ip: '192.0.2.7', ports: [] }) };
};

(async () => {
  const result = await api.fetchNodePorts('192.0.2.7');
  assert.strictEqual(result.ip, '192.0.2.7');
  assert.strictEqual(requests.length, 1);
  assert.strictEqual(requests[0].url, '/platform-api/network/nodes/192.0.2.7/ports');
  assert.strictEqual(requests[0].options.credentials, 'same-origin');
  assert.strictEqual(requests[0].options.cache, 'no-store');
  assert.ok(requests[0].options.signal, 'the single API read is bounded by a timeout');
  console.log('bigscreen Port API tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
