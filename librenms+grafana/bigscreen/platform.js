;(function () {
  'use strict';

  // Platform control helpers: pure scoring/lint logic shared by the browser UI
  // and unit tests. DOM rendering and Prometheus queries stay in app.js.

  function levelRank(level) {
    return { good: 0, info: 1, warn: 2, bad: 3 }[level] ?? 1;
  }

  function worstLevel(levels) {
    return levels.reduce((worst, level) => (levelRank(level) > levelRank(worst) ? level : worst), "good");
  }

  function readinessScore(checks) {
    if (!checks.length) return { score: 0, level: "info", bad: 0, warn: 0 };
    const bad = checks.filter((item) => item.level === "bad").length;
    const warn = checks.filter((item) => item.level === "warn").length;
    const penalty = bad * 20 + warn * 8;
    const score = Math.max(0, Math.min(100, 100 - penalty));
    const level = bad ? "bad" : warn ? "warn" : "good";
    return { score, level, bad, warn };
  }

  function envFlag(value) {
    return ["1", "true", "yes", "on"].includes(String(value || "").trim().toLowerCase());
  }

  function summarizePlayers(players, expectedSeats) {
    const seenSeats = new Map();
    players.forEach((player) => {
      const key = `${player.team}|${player.seat}`;
      if (!seenSeats.has(key)) seenSeats.set(key, []);
      seenSeats.get(key).push(player);
    });
    const online = players.filter((player) => player.success).length;
    const highLatency = players.filter((player) => player.success && Number.isFinite(player.latency) && player.latency >= 0.08).length;
    const duplicateSeats = Array.from(seenSeats.values()).filter((items) => items.length > 1).length;
    const missing = Math.max(0, Number(expectedSeats || 0) - seenSeats.size);
    return {
      total: players.length,
      seats: seenSeats.size,
      expectedSeats: Number(expectedSeats || 0),
      online,
      offline: Math.max(0, players.length - online),
      highLatency,
      duplicateSeats,
      missing
    };
  }

  function summarizeTargets(targets) {
    const byKind = { isp: 0, firewall: 0, core: 0, dist: 0, server: 0, other: 0 };
    const offline = [];
    targets.forEach((target) => {
      const job = String(target.job || "");
      const kind = job.includes("isp") ? "isp"
        : job.includes("fw") ? "firewall"
        : job.includes("core") ? "core"
        : job.includes("dist") ? "dist"
        : job.includes("srv") ? "server"
        : "other";
      byKind[kind] += 1;
      if (!target.success) offline.push(target);
    });
    return { total: targets.length, byKind, offline };
  }

  function summarizeServices(items) {
    const jobs = new Map();
    items.forEach((item) => {
      const job = item.metric && item.metric.job ? item.metric.job : item.name;
      if (!job) return;
      const prev = jobs.get(job) || { job, total: 0, up: 0 };
      prev.total += 1;
      if (item.value >= 1) prev.up += 1;
      jobs.set(job, prev);
    });
    return Array.from(jobs.values()).sort((a, b) => a.job.localeCompare(b.job));
  }

  function buildConfigRisks(config, runtimeStatus) {
    const risks = [];
    const ispNames = String(config.ispNames || "").split(",").map((item) => item.trim()).filter(Boolean);
    const ispAuto = envFlag(config.ispAutoDiscovery);

    if (!ispAuto && !ispNames.length) {
      risks.push({ level: "warn", label: "ISP 名称", value: "默认值", note: "未启用自动发现时建议显式配置 BIGSCREEN_ISP_NAMES" });
    }
    if (!String(config.ispMaxBandwidthMbps || "").trim()) {
      risks.push({ level: "warn", label: "ISP 带宽", value: "未设置", note: "饱和判断会退回默认 1000 Mbps" });
    }
    if (runtimeStatus && runtimeStatus.error) {
      risks.push({ level: "warn", label: "运行状态接口", value: "不可用", note: runtimeStatus.error });
    }
    if (runtimeStatus && runtimeStatus.targets && runtimeStatus.targets.total === 0) {
      risks.push({ level: "warn", label: "选手目标", value: "0", note: "player-targets 未生成目标或还未扫描到选手" });
    }
    return risks;
  }

  function buildTopologyFindings(targets, edges) {
    const summary = summarizeTargets(targets);
    const findings = [];
    if (summary.byKind.core === 0) {
      findings.push({ level: "bad", label: "核心设备", value: "缺失", note: "CORE_SWITCH_PING 没有有效目标" });
    }
    if (summary.byKind.firewall === 0) {
      findings.push({ level: "warn", label: "防火墙", value: "缺失", note: "FIREWALL_PING 没有有效目标" });
    }
    if (summary.byKind.dist === 0) {
      findings.push({ level: "warn", label: "接入交换机", value: "缺失", note: "DIST_SWITCH_PING 没有有效目标" });
    }
    if (summary.offline.length) {
      findings.push({
        level: "bad",
        label: "离线设备",
        value: String(summary.offline.length),
        note: summary.offline.slice(0, 4).map((item) => item.displayName || item.instance || item.targetIp).join("、")
      });
    }
    if (summary.byKind.dist > 0 && !edges.length) {
      findings.push({ level: "warn", label: "LLDP 边", value: "0", note: "拓扑只能按逻辑兜底绘制，建议确认 LLDP/SNMP" });
    }
    return findings;
  }

  function buildReadinessChecks(input) {
    const checks = [];
    const seat = input.seatSummary || summarizePlayers([], 0);
    const target = input.targetSummary || summarizeTargets([]);
    const services = input.serviceSummary || [];
    const configRisks = input.configRisks || [];
    const topologyFindings = input.topologyFindings || [];

    checks.push({
      section: "赛前",
      label: "座位识别",
      level: seat.expectedSeats && seat.missing === 0 && seat.duplicateSeats === 0 ? "good" : "warn",
      value: seat.expectedSeats ? `${seat.seats}/${seat.expectedSeats}` : String(seat.seats),
      note: seat.missing ? `缺失 ${seat.missing}` : seat.duplicateSeats ? `重复 ${seat.duplicateSeats}` : "座位匹配"
    });
    checks.push({
      section: "赛前",
      label: "选手在线",
      level: seat.offline ? "bad" : "good",
      value: `${seat.online}/${seat.total}`,
      note: seat.highLatency ? `${seat.highLatency} 个高延迟` : "在线状态正常"
    });
    checks.push({
      section: "基础设施",
      label: "核心/防火墙",
      level: target.byKind.core > 0 && target.byKind.firewall > 0 ? "good" : "warn",
      value: `${target.byKind.core}/${target.byKind.firewall}`,
      note: "核心 / 防火墙目标数"
    });
    checks.push({
      section: "基础设施",
      label: "设备离线",
      level: target.offline.length ? "bad" : "good",
      value: String(target.offline.length),
      note: target.offline.length ? target.offline.slice(0, 3).map((item) => item.displayName || item.instance || item.targetIp).join("、") : "无离线"
    });
    const downJobs = services.filter((job) => job.up < job.total);
    checks.push({
      section: "采集",
      label: "采集任务异常",
      level: downJobs.length ? "warn" : "good",
      value: downJobs.length ? `${downJobs.length} 异常` : "正常",
      note: downJobs.slice(0, 3).map((job) => `${job.job} ${job.up}/${job.total}`).join("、")
    });
    configRisks.forEach((risk) => checks.push({ section: "配置", ...risk }));
    topologyFindings.forEach((finding) => checks.push({ section: "拓扑", ...finding }));
    return checks;
  }

  function splitInterfaceBlocks(text) {
    const blocks = [];
    let current = null;
    String(text || "").split(/\r?\n/).forEach((line, index) => {
      const match = line.match(/^\s*interface\s+(.+?)\s*$/i);
      if (match) {
        if (current) blocks.push(current);
        current = { name: match[1], startLine: index + 1, lines: [] };
      } else if (current) {
        current.lines.push(line);
      }
    });
    if (current) blocks.push(current);
    return blocks.map((block) => ({ ...block, body: block.lines.join("\n") }));
  }

  function isShutdown(body) {
    return /^\s*shutdown\s*$/im.test(body);
  }

  function isLikelyUplink(block) {
    const text = `${block.name}\n${block.body}`.toLowerCase();
    return /port-channel|po\d+|uplink|to-core|to core|to-dist|to dist|trunk|core|firewall|fw|router|lag|backup/.test(text);
  }

  function addIssue(issues, level, label, note, line) {
    issues.push({ level, label, note, line: line || 0 });
  }

  function abbreviateIfPrefix(prefix) {
    const table = [
      [/^tengigabitethernet$/i, "Te"],
      [/^twentyfivegige?$/i, "Twe"],
      [/^fortygige?$/i, "Fo"],
      [/^hundredgige?$/i, "Hu"],
      [/^gigabitethernet$/i, "Gi"],
      [/^fastethernet$/i, "Fa"],
      [/^ethernet$/i, "Eth"]
    ];
    for (const [re, abbr] of table) {
      if (re.test(prefix)) return abbr;
    }
    return prefix;
  }

  // Collapse a list of interface names into compact ranges, e.g.
  // ["GigabitEthernet1/0/1".."/10"] -> "Gi1/0/1-10".
  function compressInterfaceNames(names) {
    const groups = new Map();
    const order = [];
    const leftovers = [];
    names.forEach((name) => {
      const match = String(name).match(/^([A-Za-z][A-Za-z\-]*?)\s*(\d+(?:\/\d+)*)$/);
      if (!match) { leftovers.push(String(name)); return; }
      const prefix = abbreviateIfPrefix(match[1]);
      const parts = match[2].split("/");
      const last = Number(parts.pop());
      const base = parts.join("/");
      const key = `${prefix}|${base}`;
      if (!groups.has(key)) { groups.set(key, { prefix, base, nums: [] }); order.push(key); }
      groups.get(key).nums.push(last);
    });
    const pieces = order.map((key) => {
      const group = groups.get(key);
      const nums = [...new Set(group.nums)].sort((a, b) => a - b);
      const ranges = [];
      let start = nums[0];
      let prev = nums[0];
      for (let i = 1; i < nums.length; i++) {
        if (nums[i] === prev + 1) { prev = nums[i]; continue; }
        ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
        start = prev = nums[i];
      }
      ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
      const head = group.base ? `${group.prefix}${group.base}/` : group.prefix;
      return `${head}${ranges.join(",")}`;
    });
    return pieces.concat(leftovers).join(" ") || names.join(" ");
  }

  function parseVlanList(text) {
    const set = new Set();
    String(text).split(",").forEach((part) => {
      const range = part.trim().match(/^(\d+)-(\d+)$/);
      if (range) {
        for (let i = Number(range[1]); i <= Number(range[2]); i++) set.add(i);
      } else if (/^\d+$/.test(part.trim())) {
        set.add(Number(part.trim()));
      }
    });
    return set;
  }

  function lintSwitchConfig(text) {
    const raw = String(text || "");
    if (!raw.trim()) return [];
    const issues = [];
    const blocks = splitInterfaceBlocks(raw);
    const global = raw.replace(/\n\s*interface\s+[\s\S]*$/i, "");
    const hasGlobalBpduguard = /^\s*spanning-tree\s+portfast\s+bpduguard\s+default\s*$/im.test(global);
    const hasGlobalPortfast = /^\s*spanning-tree\s+portfast\s+(?:default|edge\s+default)\s*$/im.test(global);
    const dhcpSnooping = /^\s*ip\s+dhcp\s+snooping\s*$/im.test(global) || /^\s*ip\s+dhcp\s+snooping\s+vlan\s+/im.test(global);

    if (!/^\s*no\s+vstack\s*$/im.test(global)) {
      addIssue(issues, "warn", "no vstack", "建议全局关闭 Cisco vstack，减少无用服务暴露", 0);
    }
    if (!/^\s*logging\s+(?:host\s+)?\d{1,3}(?:\.\d{1,3}){3}/im.test(raw)) {
      addIssue(issues, "warn", "日志服务器", "未看到 logging host，串线/保护关闭无法进入 Loki/飞书", 0);
    }
    if (!/^\s*errdisable\s+recovery\s+cause\s+bpduguard\s*$/im.test(global)) {
      addIssue(issues, "info", "BPDU 恢复", "未配置 errdisable recovery cause bpduguard，现场需手工 no shut", 0);
    }
    if (!/^\s*errdisable\s+recovery\s+cause\s+storm-control\s*$/im.test(global)) {
      addIssue(issues, "info", "风暴恢复", "未配置 errdisable recovery cause storm-control，广播风暴后需手工恢复", 0);
    }
    if (!/^\s*errdisable\s+recovery\s+interval\s+\d+/im.test(global)) {
      addIssue(issues, "info", "恢复间隔", "未看到 errdisable recovery interval", 0);
    }

    // Collect per-check hits across all ports, then emit ONE grouped card per
    // check (e.g. "Gi1/0/1-10 BPDU Guard") instead of one card per port.
    const groups = new Map();
    const hit = (key, level, suffix, note, block) => {
      if (!groups.has(key)) { groups.set(key, { level, suffix, note, ports: [] }); }
      groups.get(key).ports.push({ name: block.name, line: block.startLine });
    };

    const accessVlans = new Set();
    const trunkBlocks = [];

    blocks.forEach((block) => {
      const body = block.body;
      if (isShutdown(body)) return;
      const access = /switchport\s+mode\s+access/i.test(body) || (/switchport\s+access\s+vlan/i.test(body) && !/switchport\s+mode\s+trunk/i.test(body));
      const trunk = /switchport\s+mode\s+trunk|switchport\s+trunk\s+allowed|channel-group\s+\d+/i.test(body) || /^port-channel/i.test(block.name);
      const uplink = trunk || isLikelyUplink(block);

      if (uplink) {
        trunkBlocks.push(block);
      } else if (access) {
        const vlanMatch = body.match(/switchport\s+access\s+vlan\s+(\d+)/i);
        if (vlanMatch) accessVlans.add(Number(vlanMatch[1]));
      }

      if (access && !uplink) {
        if (!hasGlobalPortfast && !/spanning-tree\s+portfast(?:\s+edge)?/i.test(body)) {
          hit("portfast", "warn", "PortFast", "接入口建议启用 spanning-tree portfast edge", block);
        }
        if (!hasGlobalBpduguard && !/spanning-tree\s+bpduguard\s+enable/i.test(body)) {
          hit("bpduguard", "bad", "BPDU Guard", "接入口缺少 BPDU Guard，接错交换机时不会自动保护", block);
        }
        if (!/storm-control\s+broadcast\s+level/i.test(body)) {
          hit("storm", "warn", "广播风暴", "接入口建议配置 storm-control broadcast level", block);
        }
        if (/storm-control\s+broadcast\s+level/i.test(body) && !/storm-control\s+action\s+shutdown/i.test(body)) {
          hit("stormaction", "warn", "风暴动作", "已有广播阈值但缺少 storm-control action shutdown", block);
        }
        if (/ip\s+dhcp\s+snooping\s+trust/i.test(body)) {
          hit("dhcptrust", "bad", "DHCP Trust", "普通接入口不应配置 DHCP snooping trust", block);
        }
      }

      if (trunk && /spanning-tree\s+bpduguard\s+enable/i.test(body)) {
        hit("trunkbpdu", "bad", "BPDU Guard", "Trunk/上联口不建议开 bpduguard，会误断互联", block);
      }
      if (dhcpSnooping && uplink && !/ip\s+dhcp\s+snooping\s+trust/i.test(body)) {
        hit("uplinktrust", "info", "DHCP Trust", "已启用 DHCP Snooping，上联/DHCP 来源口通常需要 trust", block);
      }
    });

    groups.forEach((group) => {
      const label = `${compressInterfaceNames(group.ports.map((port) => port.name))} ${group.suffix}`;
      const note = group.ports.length > 1 ? `${group.note}（共 ${group.ports.length} 口）` : group.note;
      addIssue(issues, group.level, label, note, group.ports[0].line);
    });

    // 核心/分线配合：接入口用到的 VLAN 必须在上联 Trunk 放行，否则这些口的选手到不了核心。
    if (accessVlans.size && !trunkBlocks.length) {
      addIssue(issues, "info", "上联缺失", "有接入口但未发现上联 Trunk/Port-channel 口，确认与核心/分线交换机的互联", 0);
    } else if (accessVlans.size && trunkBlocks.length) {
      const allowed = new Set();
      let restricts = false;
      trunkBlocks.forEach((block) => {
        (block.body.match(/switchport\s+trunk\s+allowed\s+vlan(?:\s+add)?\s+([0-9,\-]+)/ig) || []).forEach((line) => {
          restricts = true;
          const list = line.match(/([0-9,\-]+)\s*$/);
          if (list) parseVlanList(list[1]).forEach((vlan) => allowed.add(vlan));
        });
      });
      if (restricts) {
        const missing = [...accessVlans].filter((vlan) => !allowed.has(vlan)).sort((a, b) => a - b);
        if (missing.length) {
          addIssue(issues, "bad", "上联放行 VLAN", `上联 Trunk 未放行接入 VLAN ${missing.join("、")}，这些口的选手上不了核心`, trunkBlocks[0].startLine);
        }
      }
    }

    return issues.sort((a, b) => levelRank(b.level) - levelRank(a.level) || a.line - b.line);
  }

  const ns = {
    levelRank,
    worstLevel,
    readinessScore,
    envFlag,
    summarizePlayers,
    summarizeTargets,
    summarizeServices,
    buildConfigRisks,
    buildTopologyFindings,
    buildReadinessChecks,
    splitInterfaceBlocks,
    lintSwitchConfig
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSPlatform = ns;
  }
}());
