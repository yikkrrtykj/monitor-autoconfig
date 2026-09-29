;(function () {
  'use strict';

  function portState(port) {
    if (port.adminState === 'down') return { key: 'disabled', label: '管理关闭' };
    if (port.adminState === 'up' && port.operState === 'up') return { key: 'up', label: '在线' };
    if (port.adminState === 'up' && port.operState === 'down') return { key: 'down', label: '链路断开' };
    return { key: 'unknown', label: '未知' };
  }

  function physicalParts(port) {
    const match = /^(Gi|Te|Fa|Hu|Fo|Twe|Eth)([1-9]\d?)\/(\d{1,2})\/([1-9]\d{0,2})$/.exec(port.ifName || '');
    if (!match) return null;
    const member = Number(match[2]);
    const slot = Number(match[3]);
    const number = Number(match[4]);
    if (member !== port.stackMember || number !== port.portNumber ||
        member > 16 || number > 256) return null;
    return { family: match[1], member, slot, number };
  }

  function groupPorts(ports) {
    const groups = new Map();
    const other = [];
    (Array.isArray(ports) ? ports : []).forEach((port, index) => {
      const parts = physicalParts(port);
      if (!parts) {
        other.push({ port, index });
        return;
      }
      const key = `${parts.member}:${parts.family}:${parts.slot}`;
      if (!groups.has(key)) groups.set(key, { ...parts, items: [], seen: new Set() });
      const bank = groups.get(key);
      if (bank.seen.has(parts.number)) {
        other.push({ port, index });
        return;
      }
      bank.seen.add(parts.number);
      bank.items.push({ port, index });
    });
    const banks = [...groups.values()].sort((a, b) => a.member - b.member ||
      a.family.localeCompare(b.family) || a.slot - b.slot);
    banks.forEach((bank) => {
      bank.items.sort((a, b) => a.port.portNumber - b.port.portNumber || a.index - b.index);
      bank.twoRow = bank.items.length >= 24 && Math.max(...bank.items.map((item) => item.port.portNumber)) <= 48;
      if (bank.twoRow) {
        bank.top = bank.items.filter((item) => item.port.portNumber % 2 === 1);
        bank.bottom = bank.items.filter((item) => item.port.portNumber % 2 === 0);
      }
    });
    const memberGroups = new Map();
    banks.forEach((bank) => {
      if (!memberGroups.has(bank.member)) memberGroups.set(bank.member, { member: bank.member, banks: [] });
      memberGroups.get(bank.member).banks.push(bank);
    });
    return { members: [...memberGroups.values()], banks, other };
  }

  function createPortPanel({ document, window, escapeHtml, setTimeout: delay = setTimeout,
                             clearTimeout: cancel = clearTimeout }) {
    const element = () => document.getElementById('topologyPortPanel');
    let payload = null;
    let hoverTimer = null;
    let selectedIndex = null;
    let onClose = () => {};
    const safe = (value) => escapeHtml(value == null || value === '' ? '—' : String(value));
    const number = (value, digits = 1) => Number.isFinite(value) ? Number(value).toFixed(digits) : '—';
    const speed = (value) => !Number.isFinite(value) ? '—' : value >= 1e9
      ? `${Number((value / 1e9).toFixed(3))}G` : value >= 1e6 ? `${number(value / 1e6, 0)}M` : `${number(value / 1e3, 0)}K`;
    const rate = (value) => !Number.isFinite(value) ? '—' : value >= 1e9
      ? `${number(value / 1e9)} Gb/s` : value >= 1e6 ? `${number(value / 1e6)} Mb/s`
        : value >= 1e3 ? `${number(value / 1e3)} kb/s` : `${number(value, 0)} b/s`;
    const percent = (value) => Number.isFinite(value) ? `${number(value * 100)}%` : '—';
    const row = (label, value) => `<dt>${safe(label)}</dt><dd>${safe(value)}</dd>`;
    const cumulative = (port) => ['inputErrorsTotal', 'outputErrorsTotal', 'inputDiscardsTotal', 'outputDiscardsTotal']
      .some((field) => Number.isFinite(port[field]) && port[field] > 0);

    function face(item, physical = true) {
      const { port, index } = item;
      const state = portState(port);
      return `<button type="button" class="port-face port-state-${state.key}" data-port-index="${index}" aria-label="${safe(port.ifName)} ${state.label}">
        <span class="port-face-number">${safe(physical ? port.portNumber : port.ifName)}</span>
        <span class="port-face-speed">${speed(port.speedBps)}</span>
        ${cumulative(port) ? '<span class="port-face-counter" aria-label="存在非零累计计数">·</span>' : ''}
      </button>`;
    }

    function bankHtml(bank) {
      const heading = `${bank.family} ${bank.member}/${bank.slot}`;
      const columns = bank.twoRow ? Math.max(bank.top.length, bank.bottom.length) : bank.items.length;
      const rowHtml = (items) => `<div class="port-bank-row" style="--port-columns:${Math.max(1, columns)}">${items.map((item) => face(item)).join('')}</div>`;
      return `<div class="port-bank ${bank.twoRow ? 'port-bank-wide' : ''}"><h4>${safe(heading)}</h4><div class="port-bank-rows ${bank.twoRow ? 'port-bank-two-row' : 'port-bank-compact'}">
        ${bank.twoRow ? rowHtml(bank.top) + rowHtml(bank.bottom) : rowHtml(bank.items)}
      </div></div>`;
    }

    function memberHtml(group) {
      return `<section class="port-member"><h3>成员 ${group.member}</h3><div class="port-member-banks">${group.banks.map(bankHtml).join('')}</div></section>`;
    }

    function neighborsText(neighbors) {
      if (neighbors == null) return '邻接资料暂不可用';
      if (!Array.isArray(neighbors) || neighbors.length === 0) return '当前快照未发现邻接';
      return neighbors.map((neighbor) => {
        const peer = [neighbor.peerName, neighbor.peerIp, neighbor.peerPort].filter(Boolean).join(' / ');
        const protocol = Array.isArray(neighbor.protocols) ? neighbor.protocols.join(', ') : '';
        const members = Array.isArray(neighbor.memberPorts) ? neighbor.memberPorts.join(', ') : '';
        return [peer, protocol, neighbor.aggregatePort ? `聚合 ${neighbor.aggregatePort}` : '',
          members ? `成员 ${members}` : ''].filter(Boolean).join(' · ');
      }).join('；');
    }

    function detailHtml(port) {
      const vlan = port.vlanEvidence && Array.isArray(port.vlanEvidence.memberships)
        ? port.vlanEvidence.memberships.map((item) => `${item.vlanId}${item.untagged === true ? '（未标记）' : ''}`).join(', ') || '—'
        : '—';
      return `<div class="port-detail-content"><strong>${safe(port.ifName)}</strong><dl>
        ${row('别名', port.ifAlias)}${row('描述', port.ifDescr)}
        ${row('管理状态', port.adminState)}${row('链路状态', port.operState)}
        ${row('速率', speed(port.speedBps))}${row('RX', rate(port.rxBps))}${row('TX', rate(port.txBps))}
        ${row('RX 利用率', percent(port.rxUtilization))}${row('TX 利用率', percent(port.txUtilization))}
        ${row('输入错误（累计）', port.inputErrorsTotal)}${row('输出错误（累计）', port.outputErrorsTotal)}
        ${row('输入丢弃（累计）', port.inputDiscardsTotal)}${row('输出丢弃（累计）', port.outputDiscardsTotal)}
        ${row('VLAN 观测数据 / 非配置权威', vlan)}${row('当前邻接', neighborsText(port.neighbors))}
        ${port.wanEvidence ? row('WAN / ISP', [port.wanEvidence.name,
          port.wanEvidence.wanIp && `WAN IP ${port.wanEvidence.wanIp}`,
          port.wanEvidence.gateway && `网关 ${port.wanEvidence.gateway}`].filter(Boolean).join(' · ')) : ''}
      </dl></div>`;
    }

    function interfaceRow(port, index) {
      const state = portState(port);
      const description = [port.ifAlias, port.ifDescr].filter((value, i, values) => value &&
        value !== port.ifName && values.indexOf(value) === i).join(' · ');
      return `<button type="button" class="port-face port-interface-row port-state-${state.key}" data-port-index="${index}"
        aria-label="${safe(port.ifName)} ${state.label}">
        <span class="port-interface-name">${safe(port.ifName)}</span>
        <span class="port-interface-description">${safe(description)}</span>
        <span class="port-interface-state">${state.label} · ${safe(port.adminState)}</span>
        <span>${speed(port.speedBps)}</span><span>RX ${rate(port.rxBps)} · TX ${rate(port.txBps)}</span>
        <span>${cumulative(port) ? '累计 errors/discards 非 0' : '累计 —'}</span>
      </button>`;
    }

    function interfaceSection(title, items) {
      return `<section class="port-interface-section"><h3>${safe(title)}</h3><div class="port-interface-list">${items.map(({ port, index }) => interfaceRow(port, index)).join('')}</div></section>`;
    }

    function openInterfaces(data, counts) {
      const wan = [];
      const other = [];
      data.ports.forEach((port, index) => (port.wanEvidence ? wan : other).push({ port, index }));
      const root = element();
      root.hidden = false;
      root.innerHTML = `<header><div><strong>${safe(data.name || data.ip)}</strong><p>${safe(data.model)} · ${safe(data.ip)}</p></div><button class="port-panel-close" type="button" aria-label="关闭接口面板">×</button></header>
        <div class="port-panel-summary">接口 ${data.ports.length} · 在线 ${counts.up} · 离线 ${counts.down} · 未知 ${counts.unknown} · 管理关闭 ${counts.disabled}</div>
        <div class="port-panel-legend">累计 errors/discards 非 0 是历史累计证据，不等于当前故障</div>
        ${data.degraded ? '<div class="port-panel-degraded">接口资料部分降级</div>' : ''}
        ${Array.isArray(data.warnings) && data.warnings.length ? `<div class="port-panel-warnings">${data.warnings.map((warning) => `<p>${safe(warning)}</p>`).join('')}</div>` : ''}
        <div class="port-panel-scroll">${wan.length ? interfaceSection('WAN / ISP', wan) : ''}${interfaceSection(wan.length ? '其他接口' : '接口', other)}</div>
        <aside class="port-pinned-detail" hidden></aside><div class="port-hover-card" hidden></div>`;
      installEvents();
    }

    function hideHover() {
      if (hoverTimer != null) cancel(hoverTimer);
      hoverTimer = null;
      const card = element().querySelector('.port-hover-card');
      if (card) card.hidden = true;
    }

    function showHover(button) {
      if (!payload) return;
      const index = Number(button.dataset.portIndex);
      const port = payload.ports[index];
      if (!port) return;
      const card = element().querySelector('.port-hover-card');
      if (!card) return;
      card.innerHTML = detailHtml(port);
      card.hidden = false;
      const rect = button.getBoundingClientRect();
      const width = 310;
      const height = Math.min(card.offsetHeight || 340, 500);
      card.style.left = `${Math.max(8, rect.right + width + 12 <= window.innerWidth ? rect.right + 8 : rect.left - width - 8)}px`;
      card.style.top = `${Math.max(8, Math.min(rect.top, window.innerHeight - height - 8))}px`;
    }

    function installEvents() {
      const root = element();
      const portFace = (target) => target && typeof target.closest === 'function'
        ? target.closest('.port-face') : null;
      root.onclick = (event) => {
        if (event.target.closest('.port-panel-close')) { onClose(); close(); return; }
        const button = event.target.closest('.port-face');
        if (!button || !payload) return;
        selectedIndex = Number(button.dataset.portIndex);
        const detail = root.querySelector('.port-pinned-detail');
        detail.innerHTML = detailHtml(payload.ports[selectedIndex]);
        detail.hidden = false;
        root.querySelectorAll('.port-face').forEach((faceButton) => {
          faceButton.setAttribute('aria-pressed', String(faceButton === button));
        });
      };
      root.onmouseover = (event) => {
        const button = portFace(event.target);
        if (!button || portFace(event.relatedTarget) === button) return;
        hideHover();
        hoverTimer = delay(() => { hoverTimer = null; showHover(button); }, 120);
      };
      root.onmouseout = (event) => {
        const button = portFace(event.target);
        if (button && portFace(event.relatedTarget) !== button) hideHover();
      };
      root.onfocusin = (event) => {
        const button = event.target.closest('.port-face');
        if (button) { hideHover(); showHover(button); }
      };
      root.onfocusout = (event) => {
        if (event.target.closest('.port-face')) hideHover();
      };
    }

    function loading(ip, kind = 'cisco') {
      close();
      const root = element();
      root.hidden = false;
      root.innerHTML = `<header><strong>${kind === 'hillstone' ? '接口' : '端口'}面板 · ${safe(ip)}</strong><button class="port-panel-close" type="button" aria-label="关闭面板">×</button></header><p>正在读取${kind === 'hillstone' ? '接口' : '端口'}…</p>`;
      installEvents();
    }

    function open(data) {
      if (!data || !Array.isArray(data.ports)) { failure(); return; }
      payload = data;
      selectedIndex = null;
      const counts = { up: 0, down: 0, unknown: 0, disabled: 0 };
      data.ports.forEach((port) => {
        const oper = port.operState;
        counts[oper === 'up' || oper === 'down' ? oper : 'unknown'] += 1;
        if (port.adminState === 'down') counts.disabled += 1;
      });
      if (data.kind === 'hillstone') { openInterfaces(data, counts); return; }
      const { members, banks, other } = groupPorts(data.ports);
      const physicalCount = banks.reduce((count, bank) => count + bank.items.length, 0);
      const root = element();
      root.hidden = false;
      root.innerHTML = `<header><div><strong>${safe(data.name || data.ip)}</strong><p>${safe(data.model)} · ${safe(data.ip)}</p></div><button class="port-panel-close" type="button" aria-label="关闭端口面板">×</button></header>
        <div class="port-panel-summary">接口 ${data.ports.length} · 物理可识别 ${physicalCount} · 其他 ${other.length} · 运行在线 ${counts.up} · 运行离线 ${counts.down} · 运行未知 ${counts.unknown} · 管理关闭 ${counts.disabled}</div>
        <div class="port-panel-legend" aria-label="端口状态图例">
          <span><i class="port-legend-dot port-state-up"></i>在线</span><span><i class="port-legend-dot port-state-down"></i>链路断开</span>
          <span><i class="port-legend-dot port-state-disabled"></i>管理关闭</span><span><i class="port-legend-dot port-state-unknown"></i>未知</span>
          <span><i class="port-legend-counter"></i>累计 errors/discards 非 0（不等于当前故障）</span>
        </div>
        ${data.degraded ? '<div class="port-panel-degraded">端口资料部分降级</div>' : ''}
        ${Array.isArray(data.warnings) && data.warnings.length ? `<div class="port-panel-warnings">${data.warnings.map((warning) => `<p>${safe(warning)}</p>`).join('')}</div>` : ''}
        <div class="port-panel-scroll">${members.map(memberHtml).join('')}
          <details class="port-other"><summary>其他接口 (${other.length})</summary><div class="port-other-grid">${other.map((item) => face(item, false)).join('')}</div></details>
        </div><aside class="port-pinned-detail" hidden></aside><div class="port-hover-card" hidden></div>`;
      installEvents();
    }

    function failure(message = '端口资料暂不可用') {
      payload = null;
      const root = element();
      if (root.hidden) return;
      root.innerHTML = `<header><strong>端口面板</strong><button class="port-panel-close" type="button" aria-label="关闭端口面板">×</button></header><p class="port-panel-warnings">${safe(message)}</p>`;
      installEvents();
    }

    function close() {
      if (element()) {
        hideHover();
        element().hidden = true;
        element().innerHTML = '';
      }
      payload = null;
      selectedIndex = null;
    }

    function setCloseHandler(handler) { onClose = handler; }
    function isOpen() { return Boolean(element() && !element().hidden); }
    return { loading, open, failure, close, isOpen, setCloseHandler };
  }

  const ns = { createPortPanel, groupPorts, portState };
  if (typeof module !== 'undefined' && module.exports) module.exports = ns;
  else window.BSPortPanel = ns;
}());
