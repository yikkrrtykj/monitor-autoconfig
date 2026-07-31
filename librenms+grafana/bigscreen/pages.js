(function () {
  window.BIGSCREEN_QUERIES = {
    infraPingJobs: 'infra-core-ping|infra-dist-ping|infra-fw-ping',
    pingTrend: 'max by (instance) (probe_icmp_duration_seconds{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping",phase="rtt"})',
    pingGauge: 'avg by (instance, job) (min_over_time(probe_icmp_duration_seconds{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping",phase="rtt"}[1m])) or avg by (instance, job) (quantile_over_time(0.5, probe_icmp_duration_seconds{job=~"infra-isp-ping|infra-srv-ping",phase="rtt"}[1m]))',
    uptime: 'max by (instance) (last_over_time(sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance!~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"}[25m]) / 100) or max by (instance) ((last_over_time(sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance=~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"}[25m]) / 100) unless on(target_ip) last_over_time(sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance!~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"}[25m]))',
    loss: 'max by (instance, job, target_ip) (1 - avg_over_time(probe_success{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping"}[30s]))'
  };

  window.BIGSCREEN_PAGES = [
    { id: "home", path: "/", label: "首页", title: "选择比赛人数", description: "选择当前赛制和需要查看的网络页面" },
    { id: "control", path: "/control", label: "赛事控制台", title: "赛事控制台", description: "基础配置、问题清单、拓扑诊断、配置巡检和报告导出" },
    { id: "infra", path: "/infra", label: "网络总览", title: "网络总览", description: "核心网络、丢包和 ISP 流量" },
    { id: "dhcp", path: "/dhcp", label: "DHCP", title: "DHCP 地址池", description: "按需查看核心交换机地址池使用情况" },
    { id: "evidence", path: "/latency", label: "延迟查询", title: "延迟查询", description: "按队伍座位查询延迟和断线" },
    { id: "incident", path: "/incident", label: "卡顿分析", title: "卡顿根因分析", description: "输入卡顿时间点，自动关联基础设施/同台选手/ISP 流量" },
    { id: "topology", path: "/topology", label: "网络拓扑", title: "网络拓扑图", description: "ISP → 防火墙 → 核心 → 接入交换机的实时状态拓扑" },
    { id: "wireless", path: "/wireless", label: "无线总览", title: "无线异常总览", description: "查看当前 WiFi 连接和异常" },
    { id: "match-5v5", path: "/match-5v5", label: "5v5", title: "5v5 对战", description: "舞台左 vs 舞台右", kind: "match", teams: [1, 2], teamSize: 5, trendMode: "per-seat" },
    { id: "tournament-6", path: "/tournament-6", label: "6队", title: "6 队赛", description: "6 队上下两排布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6], teamSize: 4, groups: [[1, 2, 3], [4, 5, 6]], trendMode: "groups" },
    { id: "tournament-64-2layer", path: "/tournament-64-2layer", label: "64人 2层", title: "64 人二层", description: "16 队四人布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], teamSize: 4, groups: [[9, 10, 11, 12, 13, 14, 15, 16], [1, 2, 3, 4, 5, 6, 7, 8]], trendMode: "groups" },
    { id: "tournament-64-233", path: "/tournament-64-233", label: "64人 233", title: "64 人三层 233", description: "16 队四人布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], teamSize: 4, groups: [[11, 12, 13, 14, 15, 16], [5, 6, 7, 8, 9, 10], [1, 2, 3, 4]], trendMode: "groups" },
    { id: "tournament-64-332", path: "/tournament-64-332", label: "64人 332", title: "64 人三层 332", description: "16 队四人布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], teamSize: 4, groups: [[13, 14, 15, 16], [7, 8, 9, 10, 11, 12], [1, 2, 3, 4, 5, 6]], trendMode: "groups" }
  ];

  const configurableLayoutIds = new Set([
    "tournament-64-2layer",
    "tournament-64-233",
    "tournament-64-332"
  ]);

  function parsedTeamOrders(rawOrders) {
    if (rawOrders && typeof rawOrders === "object" && !Array.isArray(rawOrders)) return rawOrders;
    if (!rawOrders || typeof rawOrders !== "string") return {};
    try {
      const parsed = JSON.parse(rawOrders);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch (error) {
      return {};
    }
  }

  function defaultTeamOrder(page) {
    const groups = Array.isArray(page && page.groups) ? page.groups : [page && page.teams || []];
    return groups.flat().map(Number);
  }

  function teamOrderForPage(page, rawOrders) {
    const fallback = defaultTeamOrder(page);
    if (!page || !configurableLayoutIds.has(page.id)) return fallback;
    const candidate = parsedTeamOrders(rawOrders)[page.id];
    const expected = [...fallback].sort((left, right) => left - right);
    if (
      !Array.isArray(candidate)
      || candidate.length !== fallback.length
      || candidate.some((team) => !Number.isInteger(Number(team)))
    ) return fallback;
    const normalized = candidate.map(Number);
    const sorted = [...normalized].sort((left, right) => left - right);
    return sorted.every((team, index) => team === expected[index]) ? normalized : fallback;
  }

  function groupsForPage(page, rawOrders) {
    const order = teamOrderForPage(page, rawOrders);
    const template = Array.isArray(page && page.groups) ? page.groups : [page && page.teams || []];
    let offset = 0;
    return template.map((group) => {
      const next = order.slice(offset, offset + group.length);
      offset += group.length;
      return next;
    });
  }

  function applyTeamOrder(page, rawOrders) {
    if (!page || !configurableLayoutIds.has(page.id)) return page;
    return {
      ...page,
      groups: groupsForPage(page, rawOrders),
      trendMode: "groups"
    };
  }

  window.BIGSCREEN_TEAM_LAYOUTS = {
    configurableLayoutIds: [...configurableLayoutIds],
    defaultTeamOrder,
    teamOrderForPage,
    groupsForPage,
    applyTeamOrder
  };
})();
