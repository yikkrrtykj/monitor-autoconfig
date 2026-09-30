const assert = require('assert');
global.window = { BIGSCREEN_CONFIG: { prometheusBaseUrl: 'https://unrelated.invalid' }, BIGSCREEN_QUERIES: {} };
const api = require('../bigscreen/api.js');
const fixedNow = 1800000000000;
Date.now = () => fixedNow;
const end = fixedNow / 1000;
let requests = [];
let responseRows = [];
global.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, json: async () => ({ status: 'success', data: { result: responseRows } }) };
};
const device = { kind: 'cisco', ip: '192.0.2.7', name: 'DO-NOT-QUERY', metricSource: 'LibreNMS poller' };
const port = { ifIndex: 7, ifName: 'NOT-A-SELECTOR', ifAlias: 'ALIAS', wanEvidence: { name: 'WAN' } };
const row = (kind, ip = device.ip, index = '7', values = [[end - 30, '0'], [end, '80']]) => ({
  metric: kind === 'hillstone' ? { job: 'firewall-snmp', instance: ip, ifIndex: index }
    : { job: 'infra-switch-ifmib', target_ip: ip, ifIndex: index }, values
});
(async () => {
  for (const kind of ['cisco', 'generic-switch', 'hillstone']) {
    requests = [];
    responseRows = [row(kind)];
    const history = await api.fetchPortHistory({ ...device, kind }, port);
    assert.strictEqual(requests.length, 2);
    const selector = kind === 'hillstone' ? 'job="firewall-snmp",instance="192.0.2.7",ifIndex="7"'
      : 'job="infra-switch-ifmib",target_ip="192.0.2.7",ifIndex="7"';
    requests.forEach(({ url, options }, index) => {
      const parsed = new URL(url, 'http://screen.local');
      assert.strictEqual(parsed.pathname, '/prometheus/api/v1/query_range');
      assert.strictEqual(parsed.searchParams.get('query'), `rate(${index === 0 ? 'ifHCInOctets' : 'ifHCOutOctets'}{${selector}}[5m]) * 8`);
      assert.strictEqual(parsed.searchParams.get('start'), String(end - 900));
      assert.strictEqual(parsed.searchParams.get('end'), String(end));
      assert.strictEqual(parsed.searchParams.get('step'), '30');
      assert.ok(options.signal);
      assert.strictEqual(options.cache, 'no-store');
    });
    assert.deepStrictEqual(history.rx, [{ t: end - 30, v: 0 }, { t: end, v: 80 }]);
    assert.deepStrictEqual(history.tx, history.rx);
    assert.strictEqual(history.source, 'Prometheus');
  }
  for (const [candidate, selected] of [[{ ...device, kind: 'unknown' }, port], [device, { ifIndex: '7' }],
    [device, { ifIndex: -1 }], [device, { ifIndex: 1.2 }], [{ ...device, ip: '192.0.2.7"}' }, port]]) {
    requests = [];
    await assert.rejects(api.fetchPortHistory(candidate, selected));
    assert.strictEqual(requests.length, 0);
  }
  responseRows = [row('cisco', '192.0.2.99'), row('cisco', device.ip, '8'),
    { ...row('cisco'), metric: { ...row('cisco').metric, job: 'other' } }];
  const empty = await api.fetchPortHistory(device, port);
  assert.deepStrictEqual(empty.rx, []);
  assert.deepStrictEqual(empty.coverage, { rx: 'empty', tx: 'empty' });
  responseRows = [row('cisco'), row('cisco', device.ip, '7', [])];
  const duplicate = await api.fetchPortHistory(device, port);
  assert.strictEqual(duplicate.coverage.rx, 'ambiguous');
  assert.deepStrictEqual(duplicate.rx, []);
  responseRows = [row('cisco', device.ip, '7', [[end, '0'], [end - 30, null], [end - 60, ''], [end - 90, '-1'], [end - 120, 'NaN'], [end + 30, '4']])];
  assert.deepStrictEqual((await api.fetchPortHistory(device, port)).rx, [{ t: end, v: 0 }]);
  requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ status: 'success', data: { result: url.includes('ifHCInOctets') ? [row('cisco')] : [] } }) };
  };
  const partial = await api.fetchPortHistory(device, port);
  assert.strictEqual(partial.coverage.rx, 'available');
  assert.strictEqual(partial.coverage.tx, 'empty');
  assert.deepStrictEqual(partial.tx, []);
  requests = [];
  global.fetch = async (url) => { requests.push(url); throw new Error('offline'); };
  await assert.rejects(api.fetchPortHistory(device, port));
  assert.strictEqual(requests.length, 2, 'two independent bounded directions, no retries');
  requests = [];
  global.fetch = async (url) => {
    requests.push(url);
    if (url.includes('ifHCOutOctets')) throw new Error('one direction unavailable');
    return { ok: true, json: async () => ({ status: 'success', data: { result: [row('cisco')] } }) };
  };
  const partlyFailed = await api.fetchPortHistory(device, port);
  assert.strictEqual(partlyFailed.coverage.rx, 'available');
  assert.strictEqual(partlyFailed.coverage.tx, 'unavailable');
  assert.strictEqual(requests.length, 2);
  requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ ok: true, source: 'LibreNMS RRD', ip: device.ip, ifIndex: 7,
      rx: [], tx: [], coverage: { rx: 'empty', tx: 'empty' } }) };
  };
  await api.fetchPortHistoryFallback(device.ip, 7);
  assert.strictEqual(requests.length, 1);
  assert.strictEqual(requests[0].url, `/platform-api/network/nodes/${device.ip}/ports/7/history`);
  assert.ok(requests[0].options.signal instanceof AbortSignal);
  await assert.rejects(api.fetchPortHistoryFallback(device.ip, '7'));
  await assert.rejects(api.fetchPortHistoryFallback('../other', 7));
  assert.strictEqual(requests.length, 1);
  global.fetch = async () => ({ ok: true, json: async () => ({ ok: true, source: 'LibreNMS RRD', ip: '192.0.2.99', ifIndex: 7, rx: [], tx: [], coverage: {} }) });
  await assert.rejects(api.fetchPortHistoryFallback(device.ip, 7));
  console.log('bigscreen port history API tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
