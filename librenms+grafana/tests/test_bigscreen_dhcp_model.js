const assert = require('assert');
const dhcpModel = require('../bigscreen/dhcp/dhcp-model.js');

assert.deepStrictEqual(
  Object.keys(dhcpModel),
  [
    'dhcpRangeAddresses',
    'compactDhcpAddresses',
    'dhcpPoolKey',
    'dhcpIpv4Number',
    'dhcpPoolMatchesSearch',
    'dhcpPoolMatchesFilter',
    'dhcpPoolSortValue',
    'compareDhcpPools',
    'buildDhcpAddressContext',
    'dhcpAddressState'
  ],
  'the DHCP model exposes only the extracted pure helpers'
);

const {
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
} = dhcpModel;

assert.strictEqual(dhcpIpv4Number('0.0.0.0'), 0);
assert.strictEqual(dhcpIpv4Number('1.2.3.4'), 16909060);
assert.strictEqual(dhcpIpv4Number('255.255.255.255'), 4294967295);
assert.strictEqual(dhcpIpv4Number('001.002.003.004'), 16909060, 'leading zeroes retain Number coercion');
assert.strictEqual(dhcpIpv4Number('+1.2.3.4'), 16909060, 'signed numeric segments retain Number coercion');
for (const value of [null, undefined, '', '1.2.3', '1.2.3.4.5', 'one.2.3.4', '1.2.3.256', '-1.2.3.4', NaN]) {
  assert.strictEqual(dhcpIpv4Number(value), null, `${String(value)} retains the current invalid IPv4 result`);
}

assert.deepStrictEqual(dhcpRangeAddresses('192.168.10.5-192.168.10.5'), ['192.168.10.5']);
assert.deepStrictEqual(dhcpRangeAddresses('001.002.003.004-001.002.003.005'), [
  '1.2.3.4', '1.2.3.5'
], 'range conversion accepts leading zeroes but normalizes the expanded addresses');
assert.deepStrictEqual(dhcpRangeAddresses('192.168.10.5'), [], 'a lone address is not a range in the current parser');
assert.deepStrictEqual(dhcpRangeAddresses('192.168.10.254-192.168.11.1'), [
  '192.168.10.254', '192.168.10.255', '192.168.11.0', '192.168.11.1'
]);
assert.deepStrictEqual(dhcpRangeAddresses('255.255.255.254-255.255.255.255'), [
  '255.255.255.254', '255.255.255.255'
]);
assert.deepStrictEqual(dhcpRangeAddresses('192.168.10.9-192.168.10.7'), []);
assert.deepStrictEqual(dhcpRangeAddresses('192.168.10.1-192.168.10.256'), []);
assert.deepStrictEqual(dhcpRangeAddresses('+1.2.3.4-1.2.3.4'), [], 'range syntax remains limited to decimal digits');
assert.deepStrictEqual(dhcpRangeAddresses(''), []);
assert.deepStrictEqual(dhcpRangeAddresses(null), []);
assert.strictEqual(dhcpRangeAddresses('0.0.0.0-0.0.15.255').length, 4096, 'the existing default limit is inclusive');
assert.deepStrictEqual(dhcpRangeAddresses('0.0.0.0-0.0.16.0'), [], 'ranges over the existing 4096-address limit are rejected');
assert.deepStrictEqual(dhcpRangeAddresses('192.168.10.1-192.168.10.2', 1), [], 'the caller-supplied limit is preserved');
assert.notStrictEqual(
  dhcpRangeAddresses('192.168.10.1-192.168.10.2'),
  dhcpRangeAddresses('192.168.10.1-192.168.10.2'),
  'range expansion creates a fresh array'
);

assert.strictEqual(compactDhcpAddresses(['192.168.10.4']), '192.168.10.4');
assert.strictEqual(compactDhcpAddresses([
  '192.168.10.4', '192.168.10.2', '192.168.10.3'
]), '192.168.10.2–192.168.10.4');
assert.strictEqual(compactDhcpAddresses([
  '192.168.10.1', '192.168.10.2', '192.168.10.4', '192.168.10.5', '192.168.10.8'
]), '192.168.10.1–192.168.10.2、192.168.10.4–192.168.10.5、192.168.10.8');
assert.strictEqual(compactDhcpAddresses([
  '192.168.10.2', 'invalid', '192.168.10.1', '192.168.10.2'
]), '192.168.10.1–192.168.10.2');
assert.strictEqual(compactDhcpAddresses([]), '');
assert.strictEqual(compactDhcpAddresses(null), '');
assert.strictEqual(compactDhcpAddresses(['001.002.003.004']), '001.002.003.004', 'compact retains the original address text');
assert.strictEqual(compactDhcpAddresses(['+1.2.3.4']), '+1.2.3.4', 'compact retains its broader Number coercion');
const compactInput = ['192.168.10.3', '192.168.10.1', '192.168.10.2'];
const compactBefore = [...compactInput];
compactDhcpAddresses(compactInput);
assert.deepStrictEqual(compactInput, compactBefore, 'compact does not sort or otherwise mutate its input array');

const keyedPool = { name: 'Stage A / 主', range: '192.168.40.1 - 192.168.40.20', extra: { keep: true } };
const keyedPoolBefore = JSON.stringify(keyedPool);
assert.strictEqual(dhcpPoolKey(keyedPool), 'Stage%20A%20%2F%20%E4%B8%BB|192.168.40.1%20-%20192.168.40.20');
assert.strictEqual(JSON.stringify(keyedPool), keyedPoolBefore, 'pool key construction does not mutate the pool');

const searchPool = {
  name: 'Stage Alpha',
  range: '192.168.40.10 - 192.168.40.20',
  mac: '00:11:22:33:44:55',
  hostname: 'player-one',
  clientName: 'caster',
  status: 'active'
};
const searchBefore = JSON.stringify(searchPool);
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, ''), true);
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, '  STAGE alpha  '), true, 'name search is trimmed and case insensitive');
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, '192.168.40.15'), true, 'an address inside the range matches');
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, '192.168.40.21'), false);
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, '192.168.40'), true, 'range text substring matching is preserved');
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, '00:11:22:33:44:55'), false, 'MAC is not currently searchable');
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, 'player-one'), false, 'hostname is not currently searchable');
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, 'caster'), false, 'client name is not currently searchable');
assert.strictEqual(dhcpPoolMatchesSearch(searchPool, 'active'), false, 'status is not currently searchable');
assert.strictEqual(dhcpPoolMatchesSearch({ name: '', range: '' }, 'anything'), false);
assert.strictEqual(JSON.stringify(searchPool), searchBefore, 'search does not mutate the pool');

const filterPool = {
  range: '192.168.40.1-192.168.40.20',
  leased: 2,
  excluded: 1,
  level: 'good'
};
const filterBefore = JSON.stringify(filterPool);
assert.strictEqual(dhcpPoolMatchesFilter(filterPool, [], 'all'), true);
assert.strictEqual(dhcpPoolMatchesFilter(filterPool, [], 'active'), true);
assert.strictEqual(dhcpPoolMatchesFilter({ ...filterPool, leased: 0 }, [], 'active'), false);
assert.strictEqual(dhcpPoolMatchesFilter(filterPool, [], 'excluded'), true);
assert.strictEqual(dhcpPoolMatchesFilter({ ...filterPool, excluded: 0 }, [], 'excluded'), false);
assert.strictEqual(dhcpPoolMatchesFilter(filterPool, ['192.168.40.10'], 'attention'), true, 'an in-range conflict needs attention');
assert.strictEqual(dhcpPoolMatchesFilter(filterPool, ['192.168.41.10'], 'attention'), false);
assert.strictEqual(dhcpPoolMatchesFilter({ ...filterPool, level: 'warn' }, [], 'attention'), true);
assert.strictEqual(dhcpPoolMatchesFilter({ ...filterPool, level: 'bad' }, [], 'attention'), true);
assert.strictEqual(dhcpPoolMatchesFilter({ ...filterPool, level: 'WARN' }, [], 'attention'), false, 'level matching remains case sensitive');
assert.strictEqual(dhcpPoolMatchesFilter(filterPool, [], 'future-filter'), true, 'unknown filters retain the all-pools fallback');
assert.strictEqual(JSON.stringify(filterPool), filterBefore, 'filtering does not mutate the pool');

assert.strictEqual(dhcpPoolSortValue({ range: '192.168.10.1-192.168.10.20' }), 3232238081);
assert.strictEqual(dhcpPoolSortValue({ range: 'invalid' }), null);
assert.strictEqual(dhcpPoolSortValue({}), null);
const pools = [
  { id: 'invalid-10', name: 'pool-10', range: 'invalid' },
  { id: 'later', name: 'later', range: '192.168.20.1-192.168.20.10' },
  { id: 'invalid-2', name: 'pool-2', range: '' },
  { id: 'first-equal', name: 'same', range: '192.168.10.1-192.168.10.10' },
  { id: 'second-equal', name: 'same', range: '192.168.10.1-192.168.10.20' }
];
const poolOrderBefore = pools.map((pool) => pool.id);
const sortedPools = [...pools].sort(compareDhcpPools);
assert.deepStrictEqual(sortedPools.map((pool) => pool.id), [
  'first-equal', 'second-equal', 'later', 'invalid-2', 'invalid-10'
], 'pools sort numerically, valid ranges first, then by numeric-aware name with stable equal items');
assert.deepStrictEqual(pools.map((pool) => pool.id), poolOrderBefore, 'the existing copy-sort call pattern leaves the source array unchanged');

const conflictSet = new Set(['192.168.40.10']);
const reservedUsed = new Set(['192.168.40.10', '192.168.40.11']);
const excluded = new Set(['192.168.40.10', '192.168.40.11', '192.168.40.12']);
const used = new Set(['192.168.40.10', '192.168.40.11', '192.168.40.12', '192.168.40.13']);
assert.strictEqual(dhcpAddressState('192.168.40.10', conflictSet, reservedUsed, excluded, used), 'conflict');
assert.strictEqual(dhcpAddressState('192.168.40.11', conflictSet, reservedUsed, excluded, used), 'reserved-used');
assert.strictEqual(dhcpAddressState('192.168.40.12', conflictSet, reservedUsed, excluded, used), 'excluded');
assert.strictEqual(dhcpAddressState('192.168.40.13', conflictSet, reservedUsed, excluded, used), 'used');
assert.strictEqual(dhcpAddressState('192.168.40.14', conflictSet, reservedUsed, excluded, used), 'pool');
assert.deepStrictEqual([...conflictSet], ['192.168.40.10'], 'address state does not mutate membership sets');
assert.deepStrictEqual([...reservedUsed], ['192.168.40.10', '192.168.40.11']);
assert.deepStrictEqual([...excluded], ['192.168.40.10', '192.168.40.11', '192.168.40.12']);
assert.deepStrictEqual([...used], ['192.168.40.10', '192.168.40.11', '192.168.40.12', '192.168.40.13']);

const addressSource = {
  pool: { excludedAddresses: ['192.168.40.10', '192.168.40.11'] },
  conflicts: ['192.168.40.12'],
  bindings: {
    bindings: [
      { ip: '192.168.40.10', detail: 'first' },
      { ip: '192.168.40.10', detail: 'last' },
      { ip: null, detail: null }
    ],
    arpEntries: [{ ip: '192.168.40.11', detail: 'arp detail' }],
    usedAddresses: ['192.168.40.10'],
    observedAddresses: ['192.168.40.11']
  }
};
const addressSourceBefore = JSON.stringify(addressSource);
const addressContext = buildDhcpAddressContext(
  addressSource.pool,
  addressSource.conflicts,
  addressSource.bindings
);
assert.deepStrictEqual([...addressContext.excluded], ['192.168.40.10', '192.168.40.11']);
assert.deepStrictEqual([...addressContext.conflictSet], ['192.168.40.12']);
assert.deepStrictEqual([...addressContext.bindingDetails], [
  ['192.168.40.10', 'last'], ['', '']
], 'binding normalization stringifies fields and keeps the last duplicate detail');
assert.deepStrictEqual([...addressContext.arpDetails], [['192.168.40.11', 'arp detail']]);
assert.deepStrictEqual([...addressContext.used], ['192.168.40.10']);
assert.deepStrictEqual([...addressContext.observed], ['192.168.40.11']);
assert.deepStrictEqual([...addressContext.reservedUsed], ['192.168.40.10', '192.168.40.11']);
assert.strictEqual(JSON.stringify(addressSource), addressSourceBefore, 'address normalization does not mutate its inputs');
assert.notStrictEqual(addressContext.excluded, addressSource.pool.excludedAddresses, 'address sets are newly allocated');
assert.deepStrictEqual(buildDhcpAddressContext({}, null, null), {
  excluded: new Set(),
  conflictSet: new Set(),
  bindingDetails: new Map(),
  arpDetails: new Map(),
  used: new Set(),
  observed: new Set(),
  reservedUsed: new Set()
}, 'missing binding and ARP payloads normalize to fresh empty collections');

console.log('bigscreen DHCP model tests passed');
