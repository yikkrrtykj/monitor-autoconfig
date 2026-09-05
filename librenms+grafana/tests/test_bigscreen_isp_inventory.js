const assert = require('assert');
const path = require('path');

const apiPath = path.resolve(__dirname, '../bigscreen/api.js');
const realDateNow = Date.now;
const realConsoleWarn = console.warn;

const productionInventory = require('./fixtures/isp/production-ha-inventory.json');
const productionManualNames = [
  'telcom-100M-长期',
  'telcom-1000M',
  'unicom-1000M',
  'MLBB-telcom-300M'
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
      const missing = query.includes('instance="192.168.9.1"') && query.includes('ifIndex="2"');
      return response(prometheusPayload(missing ? [] : ['traffic'], true));
    }
    throw new Error(`unexpected request: ${url}`);
  };

  const inventory = await api.fetchIspInventory();
  assert.strictEqual(inventory.length, 5);
  assert.deepStrictEqual(inventory[1], {
    name: 'MLBB-unicom-300M',
    metricName: 'MLBB-unicom-300M',
    metricTarget: '192.168.9.1',
    metricIfindex: '2',
    metadataConflict: false,
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

async function testMetricNameDrivesTrafficAndLegacyInventoryFallsBack() {
  const payload = [{
    targets: ['203.0.113.1'],
    labels: {
      display_name: 'ISP-A',
      metric_name: 'ethernet0/4',
      metric_target: '192.0.2.1',
      metric_ifindex: '7',
      wan_ip: '203.0.113.2',
      discovery_source: 'gateway'
    }
  }];
  let queries = [];
  let api = freshApi();
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(payload);
    queries.push(new URL(String(url), 'http://localhost').searchParams.get('query'));
    return response(prometheusPayload(['traffic'], true));
  };
  const results = await api.fetchIspTraffic();
  assert.strictEqual(results[0].name, 'ISP-A');
  assert.strictEqual(results[0].metricName, 'ethernet0/4');
  assert(queries.every((query) => query.includes('instance="192.0.2.1"')));
  assert(queries.every((query) => query.includes('ifIndex="7"')));
  assert(queries.every((query) => !query.includes('ethernet0/4')));

  api = freshApi();
  const legacy = JSON.parse(JSON.stringify(payload));
  delete legacy[0].labels.metric_name;
  delete legacy[0].labels.metric_target;
  delete legacy[0].labels.metric_ifindex;
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(legacy);
    return response(prometheusPayload([], true));
  };
  assert.strictEqual((await api.fetchIspInventory())[0].metricName, 'ISP-A');
}

async function testInterfaceWithoutGatewayStillQueriesTraffic() {
  const payload = [{
    targets: [],
    labels: {
      display_name: 'WAN without gateway',
      metric_name: 'ethernet0/8',
      metric_target: '192.0.2.10',
      metric_ifindex: '8',
      wan_ip: '198.51.100.10',
      discovery_source: 'interface_only'
    }
  }];
  const queries = [];
  const api = freshApi();
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(payload);
    queries.push(new URL(String(url), 'http://localhost').searchParams.get('query'));
    return response(prometheusPayload(['traffic'], true));
  };

  const inventory = await api.fetchIspInventory();
  assert.strictEqual(inventory.length, 1);
  assert.strictEqual(inventory[0].gateway, '');
  const traffic = await api.fetchIspTraffic();
  assert.strictEqual(traffic.length, 1);
  assert.strictEqual(traffic[0].hasTrafficData, true);
  assert(queries.every((query) => query.includes('instance="192.0.2.10"')));
  assert(queries.every((query) => query.includes('ifIndex="8"')));
}

async function testSameInterfaceNameOnDifferentTargetsUsesDistinctQueries() {
  const payload = [
    {
      targets: [],
      labels: {
        display_name: 'WAN-A', metric_name: 'wan', metric_target: '192.0.2.11', metric_ifindex: '1'
      }
    },
    {
      targets: [],
      labels: {
        display_name: 'WAN-B', metric_name: 'wan', metric_target: '192.0.2.12', metric_ifindex: '1'
      }
    }
  ];
  const queries = [];
  const api = freshApi();
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(payload);
    const query = new URL(String(url), 'http://localhost').searchParams.get('query');
    queries.push(query);
    const value = query.includes('instance="192.0.2.11"') ? '11' : '22';
    return response({
      status: 'success',
      data: { result: [{ metric: { instance: 'traffic' }, values: [[2_000_000_000, value]] }] }
    });
  };

  const traffic = await api.fetchIspTraffic();
  assert.deepStrictEqual(traffic.map((item) => item.name), ['WAN-A', 'WAN-B']);
  assert.strictEqual(queries.length, 4);
  assert.strictEqual(queries.filter((query) => query.includes('instance="192.0.2.11"')).length, 2);
  assert.strictEqual(queries.filter((query) => query.includes('instance="192.0.2.12"')).length, 2);
  assert.strictEqual(traffic[0].download.values[0].v, 11);
  assert.strictEqual(traffic[1].download.values[0].v, 22);
}

async function testConflictingManualMetadataUsesOnlyGlobalBandwidth() {
  const api = freshApi({ ispMaxBandwidthMbps: '*:1000,ISP-A:200' });
  const payload = [{
    targets: ['203.0.113.1'],
    labels: {
      display_name: 'ISP-A',
      metric_name: 'ethernet0/4',
      metadata_conflict: 'true'
    }
  }];
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(payload);
    return response(prometheusPayload([], true));
  };

  const result = (await api.fetchIspTraffic())[0];
  assert.strictEqual(result.metadataConflict, true);
  assert.strictEqual(api.ispChartMaxBps(result.name, !result.metadataConflict), 1000 * 1000 * 1000);
}

async function testTopologyFallbacks() {
  const fallbackNames = ['fallback-1', 'fallback-2'];
  const failures = [
    () => response({}, { ok: false, status: 503 }),
    () => response(null, { jsonError: new SyntaxError('invalid JSON') }),
    () => response([{ targets: 'not-an-array', labels: { display_name: 'malformed' } }])
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

async function testSuccessfulEmptyTopologyInventoryIsAuthoritative() {
  const api = freshApi({ ispNames: productionManualNames.join(',') });
  let prometheusCalls = 0;
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response([]);
    prometheusCalls += 1;
    return response(prometheusPayload(['should-not-be-used']));
  };
  assert.deepStrictEqual(await api.fetchIspInventory(), []);
  assert.strictEqual(prometheusCalls, 0);
}

async function testAutoInventoryOverridesManualCountAndManualModeStaysAuthoritative() {
  let networkCalls = 0;
  let api = freshApi({
    ispAutoDiscovery: 'false',
    ispNames: 'Manual WAN A,Manual WAN B,Manual WAN C,Manual WAN D,Manual WAN E'
  });
  global.fetch = async () => {
    networkCalls += 1;
    throw new Error('manual inventory must not use discovery');
  };
  assert.deepStrictEqual(
    (await api.fetchIspInventory()).map((item) => item.name),
    ['Manual WAN A', 'Manual WAN B', 'Manual WAN C', 'Manual WAN D', 'Manual WAN E']
  );
  assert.strictEqual(networkCalls, 0);

  api = freshApi({ ispNames: productionManualNames.join(',') });
  global.fetch = async (url) => {
    assert(isTopologyRequest(url));
    return response(productionInventory);
  };
  const autoInventory = await api.fetchIspInventory();
  assert.strictEqual(autoInventory.length, 5);
  assert(autoInventory.some((item) => item.name === 'MLBB-unicom-300M'));

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

async function testBothDiscoverySourcesFailToSafeManualFallback() {
  const api = freshApi({ ispNames: productionManualNames.join(',') });
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response({}, { ok: false, status: 503 });
    throw new Error('Prometheus unavailable');
  };
  const inventory = await api.fetchIspInventory();
  assert.deepStrictEqual(inventory.map((item) => item.name), productionManualNames);
  assert(inventory.every((item) => item.discoverySource === 'manual'));
}

async function testRejectedTrafficDirectionsPreserveInventory() {
  const api = freshApi();
  global.fetch = async (url) => {
    if (isTopologyRequest(url)) return response(productionInventory);
    const query = new URL(String(url), 'http://localhost').searchParams.get('query');
    if (query.includes('ifIndex="1"') && query.includes('ifHCOutOctets')) {
      return response({}, { ok: false, status: 500 });
    }
    if (query.includes('ifIndex="2"')) {
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
  await testMetricNameDrivesTrafficAndLegacyInventoryFallsBack();
  await testInterfaceWithoutGatewayStillQueriesTraffic();
  await testSameInterfaceNameOnDifferentTargetsUsesDistinctQueries();
  await testConflictingManualMetadataUsesOnlyGlobalBandwidth();
  await testTopologyFallbacks();
  await testSuccessfulEmptyTopologyInventoryIsAuthoritative();
  await testAutoInventoryOverridesManualCountAndManualModeStaysAuthoritative();
  await testBothDiscoverySourcesFailToSafeManualFallback();
  await testRejectedTrafficDirectionsPreserveInventory();
  console.log('bigscreen ISP inventory tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(() => {
  Date.now = realDateNow;
  console.warn = realConsoleWarn;
});
