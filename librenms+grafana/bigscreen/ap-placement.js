;(function () {
  'use strict';

  // P2-B B1: read-only AP placement list for the wireless page. Data comes only
  // from the authenticated /platform-api/network/ap-placement snapshot; this
  // module never reads the raw shadow file and never drives topology edges.

  const LEVELS = ['confirmed-direct', 'observed-attachment', 'ambiguous', 'unresolved'];
  const LEVEL_LABELS = {
    'confirmed-direct': '已确认直连',
    'observed-attachment': '已验证转发出口，物理直连未确认',
    'ambiguous': '存在多个候选或证据冲突',
    'unresolved': '暂无可靠当前位置'
  };
  const LEVEL_TONES = {
    'confirmed-direct': 'good',
    'observed-attachment': 'warn',
    'ambiguous': 'warn',
    'unresolved': 'bad'
  };
  const ONLINE_LABELS = {
    online: '在线',
    offline: '离线',
    unknown: '未知（无可靠心跳）'
  };
  const COVERAGE_LABELS = {
    complete: '完整',
    partial: '不完整',
    unknown: '未知'
  };
  const DEFAULT_INTERVAL_MS = 30000;
  const DEFAULT_STALE_AFTER_MS = 75000;
  const MAX_TEXT_LENGTH = 160;
  const MAX_DETAIL_LINES = 40;

  function isPlacementEnabled(value) {
    return ['1', 'true', 'yes', 'on'].includes(
      String(value == null ? '' : value).trim().toLowerCase()
    );
  }

  function normalizeMac(value) {
    const text = String(value == null ? '' : value).trim();
    if (!text) return '';
    const compact = text.replace(/[^0-9a-fA-F]/g, '').toLowerCase();
    if (compact.length !== 12) return '';
    return [
      compact.slice(0, 2), compact.slice(2, 4), compact.slice(4, 6),
      compact.slice(6, 8), compact.slice(8, 10), compact.slice(10, 12)
    ].join(':');
  }

  function formatAge(value) {
    if (value == null || value === '') return '未知';
    const age = Number(value);
    if (!Number.isFinite(age) || age < 0) return '未知';
    const total = Math.round(age);
    if (total < 60) return `${total} 秒`;
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
  }

  function formatClock(timestamp) {
    const seconds = Number(timestamp);
    if (!Number.isFinite(seconds) || seconds <= 0) return '未知';
    try {
      const date = new Date(seconds * 1000);
      if (!Number.isFinite(date.getTime())) return '未知';
      return date.toLocaleTimeString('zh-CN', { hour12: false });
    } catch (error) {
      return '未知';
    }
  }

  function safeErrorCode(value) {
    const text = String(value == null ? '' : value).trim();
    return /^[a-z0-9_]{1,64}$/.test(text) ? text : '';
  }

  function createApPlacementPanel(dependencies) {
    const doc = dependencies.document;
    const win = dependencies.window;
    const escapeHtml = dependencies.escapeHtml || ((value) => String(value == null ? '' : value));
    const fetchApPlacement = dependencies.fetchApPlacement;
    const canRead = dependencies.canRead || (() => Promise.resolve(false));
    const onAuthFailure = dependencies.onAuthFailure;
    const enabled = dependencies.enabled === true;
    const now = dependencies.now || (() => Date.now());
    const intervalMs = dependencies.intervalMs || DEFAULT_INTERVAL_MS;
    const staleAfterMs = dependencies.staleAfterMs || DEFAULT_STALE_AFTER_MS;

    let started = false;
    let timer = null;
    let sequence = 0;
    let inFlight = false;
    let lastSuccessAt = 0;
    let lastPayload = null;
    let searchText = '';
    let levelFilter = '';
    let bound = false;
    let visibilityBound = false;

    function getElements() {
      return {
        section: doc.getElementById('apPlacementSection'),
        status: doc.getElementById('apPlacementStatus'),
        summary: doc.getElementById('apPlacementSummary'),
        list: doc.getElementById('apPlacementList'),
        search: doc.getElementById('apPlacementSearch'),
        level: doc.getElementById('apPlacementLevelFilter'),
        refresh: doc.getElementById('apPlacementRefresh')
      };
    }

    function clipped(value) {
      const text = String(value == null ? '' : value);
      return text.length > MAX_TEXT_LENGTH ? `${text.slice(0, MAX_TEXT_LENGTH)}…` : text;
    }

    function escaped(value) {
      return escapeHtml(clipped(value));
    }

    function setStatus(message, tone) {
      const status = doc.getElementById('apPlacementStatus');
      if (!status) return;
      status.textContent = message || '';
      status.dataset.tone = tone || '';
    }

    function showSection() {
      const section = doc.getElementById('apPlacementSection');
      if (section) section.hidden = false;
    }

    function hideSection() {
      const section = doc.getElementById('apPlacementSection');
      if (section) section.hidden = true;
    }

    function clearContent(message, tone) {
      const elements = getElements();
      if (elements.list) elements.list.innerHTML = '';
      if (elements.summary) elements.summary.innerHTML = '';
      lastPayload = null;
      lastSuccessAt = 0;
      if (message != null) setStatus(message, tone);
    }

    function clear() {
      sequence += 1;
      clearContent();
      hideSection();
    }

    function rowIdentity(row) {
      const mac = normalizeMac(row.ap_mac);
      if (mac) return `mac:${mac}`;
      const ip = String(row.ap_ip == null ? '' : row.ap_ip).trim();
      return ip ? `ip:${ip}` : '';
    }

    function buildRows(payload) {
      const placements = Array.isArray(payload.placements) ? payload.placements : [];
      const seen = new Set();
      const rows = [];
      let duplicates = 0;
      placements.forEach((item) => {
        if (!item || typeof item !== 'object') return;
        const identity = rowIdentity(item);
        if (identity && seen.has(identity)) {
          duplicates += 1;
          return;
        }
        if (identity) seen.add(identity);
        rows.push(item);
      });
      rows.sort((left, right) => {
        const leftLevel = LEVELS.indexOf(left.level);
        const rightLevel = LEVELS.indexOf(right.level);
        const levelOrder = (leftLevel < 0 ? LEVELS.length : leftLevel)
          - (rightLevel < 0 ? LEVELS.length : rightLevel);
        if (levelOrder) return levelOrder;
        return String(left.ap_name || left.ap_ip || '')
          .localeCompare(String(right.ap_name || right.ap_ip || ''), 'zh-CN');
      });
      return { rows, duplicates };
    }

    function countChip(label, value, tone) {
      return `<span class="ap-placement-chip ${tone || ''}"><b>${escaped(value)}</b>${escaped(label)}</span>`;
    }

    function coverageLabel(row) {
      const status = String(row.coverage_status || 'unknown');
      return COVERAGE_LABELS[status] || COVERAGE_LABELS.unknown;
    }

    function onlineLabel(row) {
      return ONLINE_LABELS[row.online] || '未知（无可靠心跳）';
    }

    function evidenceLines(row) {
      const evidence = row.evidence && typeof row.evidence === 'object' ? row.evidence : {};
      const fdb = evidence.fdb && typeof evidence.fdb === 'object' ? evidence.fdb : {};
      const exact = evidence.exact_snmp && typeof evidence.exact_snmp === 'object'
        ? evidence.exact_snmp : {};
      const manual = evidence.manual_confirmation && typeof evidence.manual_confirmation === 'object'
        ? evidence.manual_confirmation : {};
      const direct = evidence.direct_neighbor && typeof evidence.direct_neighbor === 'object'
        ? evidence.direct_neighbor : {};
      const online = evidence.online && typeof evidence.online === 'object' ? evidence.online : {};
      const coverage = evidence.coverage && typeof evidence.coverage === 'object'
        ? evidence.coverage : {};
      const lines = [
        ['AP MAC', row.ap_mac || '—'],
        ['FDB 状态', fdb.status || '—'],
        ['FDB 候选年龄', fdb.candidate_age_seconds == null ? '—' : formatAge(fdb.candidate_age_seconds)],
        ['FDB 候选数', fdb.raw_candidates == null ? '—' : fdb.raw_candidates],
        ['Exact 状态', exact.status || '—'],
        ['Exact 验证时间', formatClock(exact.validated_at)],
        ['Exact 验证年龄', exact.validation_age_seconds == null ? '—' : formatAge(exact.validation_age_seconds)],
        ['Manual Confirmation', manual.status || '—'],
        ['Direct Neighbor', `${direct.status || '—'}${direct.source ? ` · ${direct.source}` : ''}`],
        ['Online evidence', `${online.status || '—'}${online.source ? ` · ${online.source}` : ''}`],
        ['Coverage', `${COVERAGE_LABELS[coverage.status] || COVERAGE_LABELS.unknown}`
          + (Array.isArray(coverage.reasons) && coverage.reasons.length
            ? ` · ${coverage.reasons.join('；')}` : '')]
      ];
      if (Array.isArray(row.warnings) && row.warnings.length) {
        lines.push(['Warnings', row.warnings.join('；')]);
      }
      const last = row.last_observed && typeof row.last_observed === 'object' ? row.last_observed : null;
      if (last) {
        lines.push(['历史位置（非当前）', [
          last.switch_ip || '—',
          last.switch_port || '—',
          last.vlan == null ? 'VLAN —' : `VLAN ${last.vlan}`,
          last.evidence_age_seconds == null ? '年龄 —' : `年龄 ${formatAge(last.evidence_age_seconds)}`
        ].join(' · ')]);
      }
      return lines.slice(0, MAX_DETAIL_LINES);
    }

    function fact(label, value) {
      return `<div><dt>${escaped(label)}</dt><dd>${escaped(value)}</dd></div>`;
    }

    function rowHtml(row) {
      const level = LEVELS.includes(row.level) ? row.level : 'unresolved';
      const tone = LEVEL_TONES[level] || 'warn';
      const details = evidenceLines(row)
        .map(([label, value]) => fact(label, value))
        .join('');
      const reason = row.reason ? row.reason : '—';
      return `<article class="ap-placement-row level-${escaped(level)} ${tone}" data-mac="${escaped(normalizeMac(row.ap_mac))}">
  <div class="ap-placement-row-head">
    <div class="ap-placement-identity">
      <strong>${escaped(row.ap_name || '未命名 AP')}</strong>
      <span>${escaped(row.ap_ip || '—')}</span>
    </div>
    <span class="ap-placement-level ${tone}">${escaped(LEVEL_LABELS[level] || level)}</span>
  </div>
  <dl class="ap-placement-facts">
    ${fact('交换机', row.switch_ip || '—')}
    ${fact('端口', row.switch_port || '—')}
    ${fact('VLAN', row.vlan == null ? '—' : row.vlan)}
    ${fact('在线状态', onlineLabel(row))}
    ${fact('Exact 验证年龄', row.evidence_age_seconds == null ? '—' : formatAge(row.evidence_age_seconds))}
    ${fact('FDB 候选年龄', row.evidence && row.evidence.fdb
      && row.evidence.fdb.candidate_age_seconds != null
      ? formatAge(row.evidence.fdb.candidate_age_seconds) : '—')}
    ${fact('采集覆盖', coverageLabel(row))}
    ${fact('定位原因', reason)}
  </dl>
  <details class="ap-placement-evidence">
    <summary>证据详情</summary>
    <dl class="ap-placement-detail">${details}</dl>
  </details>
</article>`;
    }

    function renderRows() {
      const list = doc.getElementById('apPlacementList');
      if (!list) return;
      if (!lastPayload) {
        list.innerHTML = '';
        return;
      }
      const { rows, duplicates } = buildRows(lastPayload);
      const needle = searchText.trim().toLowerCase();
      const filtered = rows.filter((row) => {
        if (levelFilter && row.level !== levelFilter) return false;
        if (!needle) return true;
        const haystack = [
          row.ap_name, row.ap_ip, row.switch_ip, row.switch_port, row.vlan, row.reason
        ].map((value) => String(value == null ? '' : value).toLowerCase()).join(' ');
        return haystack.includes(needle);
      });
      if (!filtered.length) {
        list.innerHTML = '<div class="ap-placement-empty">没有匹配的 AP</div>';
        return;
      }
      list.innerHTML = filtered.map(rowHtml).join('');
    }

    function renderSummary(payload) {
      const summary = doc.getElementById('apPlacementSummary');
      if (!summary) return;
      const counts = payload.counts && typeof payload.counts === 'object' ? payload.counts : {};
      const coverage = payload.coverage && typeof payload.coverage === 'object' ? payload.coverage : {};
      const coverageText = COVERAGE_LABELS[coverage.status] || COVERAGE_LABELS.unknown;
      const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
      summary.innerHTML = `
        <div class="ap-placement-chips">
          ${countChip('台 AP', counts.total == null ? 0 : counts.total, '')}
          ${countChip('已确认直连', counts.confirmed_direct == null ? 0 : counts.confirmed_direct, 'good')}
          ${countChip('已验证转发出口', counts.observed_attachment == null ? 0 : counts.observed_attachment, 'warn')}
          ${countChip('多候选/冲突', counts.ambiguous == null ? 0 : counts.ambiguous, 'warn')}
          ${countChip('暂无可靠定位', counts.unresolved == null ? 0 : counts.unresolved, 'bad')}
          ${countChip('在线/离线/未知', `${counts.online == null ? 0 : counts.online} / ${counts.offline == null ? 0 : counts.offline} / ${counts.unknown == null ? 0 : counts.unknown}`, '')}
        </div>
        <p class="ap-placement-meta">采集覆盖：${escaped(coverageText)}${coverage.status === 'partial' ? '（证据不完整）' : ''}
          · 数据时间：${escaped(formatClock(payload.generated_at))}
          · 文件年龄：${escaped(formatAge(payload.file_age_seconds))}
          ${warnings.length ? ` · 警告 ${warnings.length} 条` : ''}</p>`;
    }

    function render(payload) {
      lastPayload = payload;
      lastSuccessAt = now();
      showSection();
      const duplicates = buildRows(payload).duplicates;
      renderSummary(payload);
      renderRows();
      const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
      const parts = [warnings.length
        ? `数据已降级，存在 ${warnings.length} 条警告。`
        : `已更新 · ${formatClock(payload.generated_at)}`];
      if (duplicates) parts.push(`已忽略 ${duplicates} 个重复 AP 身份。`);
      setStatus(parts.join(' '), warnings.length ? 'warn' : '');
    }

    function failureMessage(error) {
      if (error && (error.status === 401 || error.status === 403)) {
        return { message: '登录状态已失效，请重新登录后查看 AP 定位。', tone: 'bad', auth: true };
      }
      const code = safeErrorCode(error && error.payload && error.payload.code);
      if (code) return { message: `AP 定位数据不可用（${code}）。`, tone: 'bad', auth: false };
      return { message: 'AP 定位请求失败，请稍后重试。', tone: 'bad', auth: false };
    }

    async function refresh() {
      if (!enabled || !started || inFlight) return;
      const elements = getElements();
      if (!elements.section) return;
      const token = ++sequence;
      inFlight = true;
      try {
        let authenticated = false;
        try {
          authenticated = (await canRead()) === true;
        } catch (error) {
          authenticated = false;
        }
        if (token !== sequence || !started) return;
        if (!authenticated) {
          clearContent();
          hideSection();
          return;
        }
        showSection();
        if (!lastPayload) setStatus('正在读取 AP 定位…', 'loading');
        const payload = await fetchApPlacement();
        if (token !== sequence || !started) return;
        if (!payload || typeof payload !== 'object' || !Array.isArray(payload.placements)) {
          throw new Error('malformed ap placement payload');
        }
        render(payload);
      } catch (error) {
        if (token !== sequence || !started) return;
        clearContent();
        const failure = failureMessage(error);
        showSection();
        setStatus(failure.message, failure.tone);
        if (failure.auth && typeof onAuthFailure === 'function') onAuthFailure();
      } finally {
        inFlight = false;
      }
    }

    function clearTimer() {
      if (timer) {
        win.clearInterval(timer);
        timer = null;
      }
    }

    function onVisibilityChange() {
      if (!started) return;
      if (doc.hidden) {
        clearTimer();
        return;
      }
      if (lastSuccessAt && now() - lastSuccessAt > staleAfterMs) {
        clearContent('数据已过期，正在重新读取…', 'loading');
      }
      refresh();
      if (!timer) timer = win.setInterval(refresh, intervalMs);
    }

    function bindOnce(elements) {
      if (bound) return;
      bound = true;
      if (elements.search) {
        elements.search.addEventListener('input', () => {
          searchText = String(elements.search.value || '');
          renderRows();
        });
      }
      if (elements.level) {
        elements.level.addEventListener('change', () => {
          levelFilter = String(elements.level.value || '');
          renderRows();
        });
      }
      if (elements.refresh) {
        elements.refresh.addEventListener('click', () => {
          refresh();
        });
      }
    }

    function start() {
      if (!enabled) return;
      const elements = getElements();
      if (!elements.section) return;
      bindOnce(elements);
      if (started) {
        refresh();
        return;
      }
      started = true;
      if (!visibilityBound) {
        doc.addEventListener('visibilitychange', onVisibilityChange);
        visibilityBound = true;
      }
      refresh();
      clearTimer();
      timer = win.setInterval(refresh, intervalMs);
    }

    function stop() {
      started = false;
      sequence += 1;
      clearTimer();
      if (visibilityBound) {
        doc.removeEventListener('visibilitychange', onVisibilityChange);
        visibilityBound = false;
      }
      clearContent();
      hideSection();
    }

    return {
      start,
      stop,
      refresh,
      clear,
      hasScheduledRefresh: () => Boolean(timer)
    };
  }

  const ns = { createApPlacementPanel, isPlacementEnabled, normalizeMac, formatAge };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSApPlacement = ns;
  }
}());
