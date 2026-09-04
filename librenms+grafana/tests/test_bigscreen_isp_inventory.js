const assert = require('assert');
const path = require('path');

const apiPath = path.resolve(__dirname, '../bigscreen/api.js');
const realDateNow = Date.now;
const realConsoleWarn = console.warn;

const productionInventory = [
  {
    targets: ['58.40.218.213'],
    labels: {
      display_name: 'MLBB-telcom-300M',
      wan_ip: '58.40.218.214',
      discovery_source: 'subnet_gateway'
    }
  },
  {
    targets: ['210.22.142.9'],
    labels: {
      display_name: 'MLBB-unicom-300M',
      wan_ip: '210.22.142.10',
      discovery_source: 'subnet_gateway'
    }
  },
  {
    targets: ['116.238.242.153'],
    labels: {
      display_name: 'telcom-1000M',
      wan_ip: '116.238.242.155',
      discovery_source: 'subnet_gateway'
    }
  },
  {
    targets: ['101.95.176.197'],
    labels: {
      display_name: 'telcom-100M-长期',
      wan_ip: '101.95.176.198',
      discovery_source: 'subnet_gateway'
    }
  },
  {
    targets: ['116.128.201.225'],
    labels: {
      display_name: 'unicom-1000M',
      wan_ip: '116.128.201.226',
      discovery_source: 'subnet_gateway'
    }
  }
];

function freshApi(config = {}) {
  global.window = {
    BIGSCREEN_CONFIG: { ispAutoDiscovery: 'true', ispNames: '', ...config },
    BIGSCREEN_QUERIES: {}
  };
  delete require.cache[apiPath];
  return require(apiPath);
}

function response(payload, { ok = true, status = 200, jsonError = null } = {}) {
  return {
    ok,
    status,
    async json() {
      if (jsonError) throw jsonError;
      return payload;
    }
  };
}

function prometheusPayload(names, range = false) {
  return {
    status: 'success',
    data: {
      result: names.map((name, index) => range
        ? { metric: { instance: name }, values: [[2_000_000_000, String(index + 1)]] }
        : { metric: { ifAlias: name, ifIndex: String(index + 1) }, value: [2_000_000_000, '1'] })
    }
  };
}

function isTopologyRequest(url) {
  return new URL(String(url), 'http://localhost').pathname === '/topology/isp_targets.json';
}

function isRangeRequest(url) {
  return new URL(String(url), 'http://localhost').pathname.endsWith('/api/v1/query_range');
}

async function testProductionInventoryAndMissingTraffic() {
  const api = freshApi();
  let topologyCalls = 0;
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) {
      topologyCalls += 1;
      return response(productionInventory);
    }
    if (isRangeRequest(url)) {
      const query = new URL(String(url), 'http://localhost').searchParams.get('query');
      const missing = query.includes('MLBB-unicom-300M');
      return response(prometheusPayload(missing ? [] : ['traffic'], true));
    }
    throw new Error(`unexpected request: ${url}`);
  };

  const inventory = await api.fetchIspInventory();
  assert.strictEqual(inventory.length, 5);
  assert.deepStrictEqual(inventory[1], {
    name: 'MLBB-unicom-300M',
    gateway: '210.22.142.9',
    wanIp: '210.22.142.10',
    discoverySource: 'subnet_gateway'
  });
  assert.deepStrictEqual(await api.fetchIspNames(), productionInventory.map((item) => item.labels.display_name));

  const results = await api.fetchIspTraffic();
  assert.strictEqual(results.length, 5, 'inventory count must not depend on traffic series');
  const noTraffic = results.find((item) => item.name === 'MLBB-unicom-300M');
  assert(noTraffic, 'the fifth production ISP must remain present');
  assert.strictEqual(noTraffic.hasTrafficData, false);
  assert.deepStrictEqual(noTraffic.download.values, []);
  assert.deepStrictEqual(noTraffic.upload.values, []);
  assert.strictEqual(results.filter((item) => item.hasTrafficData).length, 4);
  assert.strictEqual(topologyCalls, 1, 'inventory is shared and cached across names and traffic');
}

async function testTopologyFallbacks() {
  const fallbackNames = ['fallback-1', 'fallback-2'];
  const failures = [
    () => response({}, { ok: false, status: 503 }),
    () => response(null, { jsonError: new SyntaxError('invalid JSON') }),
    () => response([]),
    () => response([{ targets: [], labels: { display_name: 'malformed' } }])
  ];

  for (const topologyResponse of failures) {
    const api = freshApi();
    let prometheusCalls = 0;
    global.fetch = async (url) => {
      if (isTopologyRequest(url)) return topologyResponse();
      prometheusCalls += 1;
      return response(prometheusPayload(fallbackNames));
    };
    const inventory = await api.fetchIspInventory();
    assert.deepStrictEqual(inventory.map((item) => item.name), fallbackNames);
    assert(inventory.every((item) => item.discoverySource === 'prometheus'));
    assert.strictEqual(prometheusCalls, 1);
  }
}

async function testManualPriorityAndInventoryRefresh() {
  let networkCalls = 0;
  let api = freshApi({ ispNames: 'Manual WAN A,Manual WAN B' });
  global.fetch = async () => {
    networkCalls += 1;
    throw new Error('manual inventory must not use discovery');
  };
  assert.deepStrictEqual(
    (await api.fetchIspInventory()).map((item) => item.name),
    ['Manual WAN A', 'Manual WAN B']
  );
  assert.strictEqual(networkCalls, 0);

  let now = 1_000_000;
  Date.now = () => now;
  let topologyCalls = 0;
  api = freshApi();
  global.fetch = async (url) => {
    assert(isTopologyRequest(url));
    topologyCalls += 1;
    const payload = topologyCalls === 1 ? productionInventory.slice(0, 4) : productionInventory;
    return response(payload);
  };
  assert.strictEqual((await api.fetchIspInventory()).length, 4);
  now += 59_000;
  assert.strictEqual((await api.fetchIspInventory()).length, 4);
  assert.strictEqual(topologyCalls, 1);
  now += 1_001;
  assert.strictEqual((await api.fetchIspInventory()).length, 5);
  assert.strictEqual(topologyCalls, 2, 'topology inventory refreshes without a page reload');
  Date.now = realDateNow;
}

async function testRejectedTrafficDirectionsPreserveInventory() {
  const api = freshApi();
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(productionInventory);
    const query = new URL(String(url), 'http://localhost').searchParams.get('query');
    if (query.includes('MLBB-telcom-300M') && query.includes('ifHCOutOctets')) {
      return response({}, { ok: false, status: 500 });
    }
    if (query.includes('MLBB-unicom-300M')) {
      return response({}, { ok: false, status: 500 });
    }
    return response(prometheusPayload(['traffic'], true));
  };

  const results = await api.fetchIspTraffic();
  assert.strictEqual(results.length, 5);
  const oneDirection = results.find((item) => item.name === 'MLBB-telcom-300M');
  assert(oneDirection.download.values.length > 0);
  assert.deepStrictEqual(oneDirection.upload.values, []);
  assert.strictEqual(oneDirection.hasTrafficData, true);
  const bothRejected = results.find((item) => item.name === 'MLBB-unicom-300M');
  assert.deepStrictEqual(bothRejected.download.values, []);
  assert.deepStrictEqual(bothRejected.upload.values, []);
  assert.strictEqual(bothRejected.hasTrafficData, false);
}

(async () => {
  console.warn = () => {};
  await testProductionInventoryAndMissingTraffic();
  await testTopologyFallbacks();
  await testManualPriorityAndInventoryRefresh();
  await testRejectedTrafficDirectionsPreserveInventory();
  console.log('bigscreen ISP inventory tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(() => {
  Date.now = realDateNow;
  console.warn = realConsoleWarn;
});
