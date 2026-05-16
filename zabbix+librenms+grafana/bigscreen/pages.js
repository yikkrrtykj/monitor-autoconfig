(function () {
  window.BIGSCREEN_QUERIES = {
    infraPingJobs: 'infra-core-ping|infra-dist-ping|infra-fw-ping',
    pingTrend: 'avg by (instance) (avg_over_time(probe_icmp_duration_seconds{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping",phase="rtt"}[3m]))',
    pingGauge: 'avg by (instance) (quantile_over_time(0.5, probe_icmp_duration_seconds{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping",phase="rtt"}[1m]))',
    uptime: 'max by (instance) (sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance!~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"} / 100) or max by (instance) ((sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance=~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"} / 100) unless on(target_ip) sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance!~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"})',
    loss: 'max by (instance) (1 - probe_success{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping"})'
  };

  window.BIGSCREEN_PAGES = [
    { id: "home", path: "/", label: "首页", title: "选择比赛人数", description: "选择当前赛制和需要查看的网络页面" },
    { id: "infra", path: "/infra", label: "网络总览", title: "网络总览", description: "核心网络、丢包和 ISP 流量" },
    { id: "evidence", path: "/latency", label: "延迟查询", title: "延迟查询", description: "按队伍座位查询延迟和断线" },
    { id: "wireless", path: "/wireless", label: "无线总览", title: "无线异常总览", description: "查看当前 WiFi 连接和异常" },
    { id: "seat-check", path: "/seat-check", label: "座位核对", title: "赛前座位核对", description: "按赛制核对队伍座位在线" },
    { id: "match-5v5", path: "/match-5v5", label: "5v5", title: "5v5 对战", description: "舞台左 vs 舞台右", kind: "match", teams: [1, 2], teamSize: 5 },
    { id: "tournament-6", path: "/tournament-6", label: "6队", title: "6 队赛", description: "6 队上下两排布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6], teamSize: 4, groups: [[1, 2, 3], [4, 5, 6]] },
    { id: "tournament-64-2layer", path: "/tournament-64-2layer", label: "64人 2层", title: "64 人二层", description: "16 队四人布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], teamSize: 4, groups: [[9, 10, 11, 12, 13, 14, 15, 16], [1, 2, 3, 4, 5, 6, 7, 8]] },
    { id: "tournament-64-233", path: "/tournament-64-233", label: "64人 233", title: "64 人三层 233", description: "16 队四人布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], teamSize: 4, groups: [[11, 12, 13, 14, 15, 16], [5, 6, 7, 8, 9, 10], [1, 2, 3, 4]] },
    { id: "tournament-64-332", path: "/tournament-64-332", label: "64人 332", title: "64 人三层 332", description: "16 队四人布局", kind: "tournament", teams: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16], teamSize: 4, groups: [[13, 14, 15, 16], [7, 8, 9, 10, 11, 12], [1, 2, 3, 4, 5, 6]] }
  ];
})();
