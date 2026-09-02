const assert = require('assert');
const configModel = require('../bigscreen/config/config-model.js');

assert.deepStrictEqual(
  Object.keys(configModel),
  [
    'cloneControlConfig',
    'asConfigArray',
    'configScalar',
    'csvText',
    'splitConfigList',
    'controlConfigDefaults',
    'configPathGet',
    'configPathSet',
    'expandIpRangeText'
  ],
  'the config model exposes only the extracted pure configuration helpers'
);

const {
  cloneControlConfig,
  asConfigArray,
  configScalar,
  csvText,
  splitConfigList,
  controlConfigDefaults,
  configPathGet,
  configPathSet,
  expandIpRangeText
} = configModel;

const emptyDefaults = controlConfigDefaults();
assert.deepStrictEqual(emptyDefaults, {
  event: {
    name: '',
    default_layout: 'tournament-64-2layer',
    team_orders: {}
  },
  networks: {
    player_vlan: 40,
    wireless_vlan: 41,
    firewall_management_ranges: '',
    player_gateways: ''
  },
  snmp: { community: 'global' },
  devices: {
    switches: [],
    servers: [],
    core: {},
    firewall: { snmp: '' },
    stage_switches: [],
    access_switches: []
  },
  isp: {
    auto_discovery: true,
    wan_if_filter: 'telecom,telcom,unicom,isp,WAN',
    max_bandwidth_mbps: '',
    links: []
  },
  unifi: {
    enabled: false,
    password: '',
    sites: 'all',
    verify_ssl: false
  },
  alerts: {
    syslog_alert_types: 'native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback',
    gateway_macs: '',
    gateway_uplink_ports: '',
    mac_flap_window_seconds: 60,
    mac_flap_threshold: 3,
    cpu_alert_percent: 70,
    memory_alert_percent: 80
  },
  security: { grafana_anonymous: true }
}, 'empty input receives the complete existing editor defaults');

const eventConfig = controlConfigDefaults({
  event: {
    name: 'Finals',
    default_layout: 'custom-layout',
    team_orders: { 'custom-layout': [4, 3, 2, 1] }
  }
});
assert.deepStrictEqual(eventConfig.event, {
  name: 'Finals',
  default_layout: 'custom-layout',
  team_orders: { 'custom-layout': [4, 3, 2, 1] }
});
assert.deepStrictEqual(
  controlConfigDefaults({ event: { team_orders: [] } }).event.team_orders,
  {},
  'invalid team order containers retain the current empty-object fallback'
);

const legacySwitch = { name: 'stage-a', target: '192.168.10.11', vendor: 'cisco' };
const migratedSwitches = controlConfigDefaults({ devices: { switches: [legacySwitch] } });
assert.deepStrictEqual(migratedSwitches.devices.stage_switches, [{
  name: 'stage-a',
  target: '192.168.10.11',
  vendor: 'cisco',
  ip: '192.168.10.11'
}], 'legacy switches populate stage switches and target is mapped to ip');
assert.deepStrictEqual(
  controlConfigDefaults({ devices: { switches: [legacySwitch], stage_switches: [] } }).devices.stage_switches,
  [],
  'an explicitly empty stage switch list does not fall back to legacy switches'
);

const targetMappings = controlConfigDefaults({
  devices: {
    stage_switches: [{ target: '192.168.10.21', extra: 'kept' }],
    access_switches: [{ name: 'access-a', target: '192.168.10.31', extra: 'kept' }],
    servers: [{ name: 'app', target: '192.168.41.20', extra: 'discarded' }]
  }
});
assert.deepStrictEqual(targetMappings.devices.stage_switches, [{
  target: '192.168.10.21', extra: 'kept', name: '', ip: '192.168.10.21'
}]);
assert.deepStrictEqual(targetMappings.devices.access_switches, [{
  name: 'access-a', target: '192.168.10.31', extra: 'kept', ip: '192.168.10.31'
}]);
assert.deepStrictEqual(targetMappings.devices.servers, [{ name: 'app', ip: '192.168.41.20' }]);

const legacyFirewall = controlConfigDefaults({ devices: { firewall: { snmp: '192.168.9.1' } } });
assert.deepStrictEqual(legacyFirewall.devices.firewall, { ip: '192.168.9.1', snmp: '' });
const separateFirewallSnmp = controlConfigDefaults({
  devices: { firewall: { ip: '192.168.9.1', snmp: '192.168.9.2' } }
});
assert.deepStrictEqual(separateFirewallSnmp.devices.firewall, {
  ip: '192.168.9.1', snmp: '192.168.9.2'
});

const cleanedEvent = controlConfigDefaults({
  event: {
    name: '武汉斗鱼嘉年华',
    security_mode: 'public',
    public_base_url: 'https://legacy.example'
  },
  networks: { player_gateways: '192.168.10.254' },
  devices: { core: { name: 'Core', ip: '192.168.10.254' } }
});
assert.strictEqual(cleanedEvent.event.name, '');
assert.strictEqual(Object.hasOwn(cleanedEvent.event, 'security_mode'), false);
assert.strictEqual(Object.hasOwn(cleanedEvent.event, 'public_base_url'), false);
assert.strictEqual(cleanedEvent.devices.core.name, '');
assert.strictEqual(cleanedEvent.networks.player_gateways, '');

[
  { name: 'grafana', ip: '192.168.41.253' },
  { name: 'Game Server', ip: '192.168.41.253' },
  { name: 'Game Server', ip: '' }
].forEach((server) => {
  assert.deepStrictEqual(
    controlConfigDefaults({ devices: { servers: [server] } }).devices.servers,
    [],
    `historical placeholder server ${JSON.stringify(server)} is removed`
  );
});
assert.deepStrictEqual(
  controlConfigDefaults({ devices: { servers: [{ name: 'real', ip: '192.168.41.253' }] } }).devices.servers,
  [{ name: 'real', ip: '192.168.41.253' }],
  'a real server using the historical address is retained'
);

const ispDefaults = controlConfigDefaults({ isp: {} }).isp;
assert.deepStrictEqual(ispDefaults, {
  auto_discovery: true,
  wan_if_filter: 'telecom,telcom,unicom,isp,WAN',
  max_bandwidth_mbps: '',
  links: []
});
const configuredIsp = controlConfigDefaults({
  isp: {
    auto_discovery: false,
    wan_if_filter: 'WAN',
    max_bandwidth_mbps: 1000,
    links: [{ name: 'primary', bandwidth_mbps: '1000/100', extra: 'kept' }]
  }
}).isp;
assert.deepStrictEqual(configuredIsp, {
  auto_discovery: false,
  wan_if_filter: 'WAN',
  max_bandwidth_mbps: 1000,
  links: [{ name: 'primary', bandwidth_mbps: '1000/100', extra: 'kept' }]
});

assert.deepStrictEqual(controlConfigDefaults({ unifi: { enabled: true, user: 'admin' } }).unifi, {
  enabled: true,
  password: '',
  sites: 'all',
  verify_ssl: false,
  user: 'admin'
});

const alertDefaults = controlConfigDefaults({ alerts: {} }).alerts;
assert.strictEqual(alertDefaults.mac_flap_window_seconds, 60);
assert.strictEqual(alertDefaults.mac_flap_threshold, 3);
assert.strictEqual(alertDefaults.cpu_alert_percent, 70);
assert.strictEqual(alertDefaults.memory_alert_percent, 80);
assert.strictEqual(
  controlConfigDefaults({
    alerts: { syslog_alert_types: 'native_vlan_mismatch,errdisable,bpduguard,loopback' }
  }).alerts.syslog_alert_types,
  'native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback',
  'the legacy alert list gains mac_flap'
);
assert.strictEqual(
  controlConfigDefaults({ alerts: { syslog_alert_types: 'custom' } }).alerts.syslog_alert_types,
  'custom',
  'custom alert lists are preserved'
);

assert.strictEqual(controlConfigDefaults({ security: {} }).security.grafana_anonymous, true);
assert.strictEqual(
  controlConfigDefaults({ security: { grafana_anonymous: false } }).security.grafana_anonymous,
  false,
  'an explicit false Grafana anonymous setting is preserved'
);

const source = {
  schema_version: 7,
  future_section: { nested: ['keep', { value: 1 }] },
  event: { name: 'Immutable source' },
  devices: { stage_switches: [{ name: 'one', ip: '192.168.10.1' }] }
};
const sourceBefore = JSON.stringify(source);
const normalized = controlConfigDefaults(source);
assert.strictEqual(JSON.stringify(source), sourceBefore, 'controlConfigDefaults does not mutate its input');
assert.deepStrictEqual(normalized.future_section, source.future_section, 'unknown top-level fields are preserved');
assert.notStrictEqual(normalized.future_section, source.future_section, 'preserved unknown fields are independently cloned');
normalized.future_section.nested[1].value = 2;
assert.strictEqual(source.future_section.nested[1].value, 1, 'mutating normalized output cannot affect the input');

const cloned = cloneControlConfig({ nested: { list: [1, 2] } });
cloned.nested.list.push(3);
assert.deepStrictEqual(cloned, { nested: { list: [1, 2, 3] } });
assert.deepStrictEqual(cloneControlConfig(null), {});

const existingArray = ['one'];
assert.strictEqual(asConfigArray(existingArray), existingArray, 'existing arrays are returned unchanged');
assert.deepStrictEqual(asConfigArray('one'), []);
assert.deepStrictEqual(asConfigArray(null), []);

assert.strictEqual(configScalar(null), '');
assert.strictEqual(configScalar(undefined), '');
assert.strictEqual(configScalar(['one', 'two']), 'one\ntwo');
assert.strictEqual(configScalar({ value: 1 }), '');
assert.strictEqual(configScalar(false), 'false');
assert.strictEqual(configScalar(0), '0');
assert.strictEqual(csvText(['one', 'two']), 'one\ntwo');
assert.strictEqual(csvText('one,two'), 'one,two');
assert.deepStrictEqual(splitConfigList(' one, two\nthree ,, '), ['one', 'two', 'three']);
assert.deepStrictEqual(splitConfigList(null), []);

const pathObject = { event: { name: 'Finals' } };
assert.strictEqual(configPathGet(pathObject, 'event.name'), 'Finals');
assert.strictEqual(configPathGet(pathObject, 'event.missing'), undefined);
assert.strictEqual(configPathSet(pathObject, 'devices.core.ip', '192.168.10.254'), undefined);
assert.deepStrictEqual(pathObject, {
  event: { name: 'Finals' },
  devices: { core: { ip: '192.168.10.254' } }
}, 'configPathSet intentionally mutates the supplied model');

assert.deepStrictEqual(expandIpRangeText('192.168.10.1-192.168.10.3'), [
  '192.168.10.1', '192.168.10.2', '192.168.10.3'
]);
assert.deepStrictEqual(expandIpRangeText('192.168.10.7-9'), [
  '192.168.10.7', '192.168.10.8', '192.168.10.9'
]);
assert.deepStrictEqual(expandIpRangeText('192.168.10.5-5'), ['192.168.10.5']);
assert.deepStrictEqual(expandIpRangeText('192.168.10.254-256'), [
  '192.168.10.254', '192.168.10.255', '192.168.10.256'
], 'range expansion remains a compatibility transform and does not add IP validation');
assert.deepStrictEqual(expandIpRangeText('192.168.10.3-192.168.11.4'), [
  '192.168.10.3-192.168.11.4'
]);
assert.deepStrictEqual(expandIpRangeText('192.168.10.9-7'), ['192.168.10.9-7']);
assert.deepStrictEqual(expandIpRangeText('invalid, 192.168.10.1'), ['invalid', '192.168.10.1']);
assert.deepStrictEqual(expandIpRangeText(''), []);

console.log('bigscreen config model tests passed');
