;(function () {
  'use strict';

  function createConfigEditor(dependencies) {
    const {
      document,
      window,
      HTMLInputElement,
      pages,
      teamLayouts,
      escapeHtml,
      controlItemHtml,
      model,
      fetchPlatformConfig,
      fetchApplyStatus,
      postPlatform,
      saveDhcpSettings,
      testDhcpConnection,
      waitForApplyRecovery,
      applyRecoveryRenderPayload,
      applyRequestTimeoutMs: APPLY_REQUEST_TIMEOUT_MS,
      onRefresh
    } = dependencies;
    const {
      configScalar,
      csvText,
      splitConfigList,
      controlConfigDefaults,
      configPathGet,
      configPathSet,
      expandIpRangeText
    } = model;
    let lastPlatformConfig = null;
    let lastDhcpSettings = null;
    let lastEditableConfig = null;
    let configResultSticky = false;
    let applyInProgress = false;

    function renderControlDhcpSettings(settings) {
      lastDhcpSettings = settings;
      const editor = document.getElementById("controlConfigForm");
      const form = document.getElementById("controlDhcpSettingsForm");
      const username = document.getElementById("controlDhcpUsername");
      const port = document.getElementById("controlDhcpPort");
      const password = document.getElementById("controlDhcpPassword");
      const enablePassword = document.getElementById("controlDhcpEnablePassword");
      const state = document.getElementById("controlDhcpSavedState");
      if (!form) return;
      if (!settings || !settings.ok) {
        if (state) state.textContent = (settings && settings.error) || "无法读取 Telnet 配置";
        return;
      }
      if (!editor || !editor.dataset.telnetDirty) {
        if (username) username.value = settings.username || "";
        if (port) port.value = String(settings.port || 23);
        if (password) password.value = "";
        if (enablePassword) enablePassword.value = "";
      }
      if (password) {
        password.placeholder = settings.passwordConfigured
          ? "已保存；留空保留原密码"
          : "尚未设置登录密码";
      }
      if (enablePassword) {
        enablePassword.placeholder = settings.enablePasswordConfigured
          ? "已保存；留空保留原密码"
          : "没有 Enable 密码可留空";
      }
      if (state) {
        const passwordState = settings.passwordConfigured ? "登录密码已保存" : "登录密码未设置";
        const enableState = settings.enablePasswordConfigured ? "Enable 密码已保存" : "未设置 Enable 密码";
        state.textContent = `${passwordState} · ${enableState}`;
      }
    }

    async function saveAndTestControlDhcpSettings(event) {
      event.preventDefault();
      const form = document.getElementById("controlDhcpSettingsForm");
      const button = document.getElementById("controlDhcpSaveTest");
      const result = document.getElementById("controlDhcpSettingsResult");
      const username = document.getElementById("controlDhcpUsername");
      const password = document.getElementById("controlDhcpPassword");
      const enablePassword = document.getElementById("controlDhcpEnablePassword");
      const port = document.getElementById("controlDhcpPort");
      const editor = document.getElementById("controlConfigForm");
      if (!form || !result) return;
      const credentials = {
        username: username ? username.value.trim() : "",
        password: password ? password.value : "",
        enablePassword: enablePassword ? enablePassword.value : "",
        port: port ? port.value : "23"
      };
      let settingsSaved = false;
      if (button) button.disabled = true;
      result.hidden = false;
      result.className = "network-tool-result loading";
      result.textContent = "正在保存当前基础配置和 Telnet 信息…";
      try {
        const configPayload = collectControlConfigForm();
        const savedConfig = await postPlatform("/config/save", {
          text: JSON.stringify(configPayload, null, 2),
          actor: "web",
          note: "save core config before Telnet test"
        });
        if (!savedConfig || !savedConfig.ok) {
          if (savedConfig) renderConfigResult({ ...savedConfig, action: "save" });
          throw new Error((savedConfig && savedConfig.error) || "基础配置验证未通过");
        }
        lastPlatformConfig = savedConfig;
        lastEditableConfig = controlConfigDefaults(savedConfig.config || configPayload);
        if (editor) delete editor.dataset.dirty;
        configResultSticky = true;
        renderConfigResult({ ...savedConfig, action: "save" });

        const settings = await saveDhcpSettings(credentials);
        settingsSaved = true;
        if (password) password.value = "";
        if (enablePassword) enablePassword.value = "";
        if (editor) delete editor.dataset.telnetDirty;
        renderControlDhcpSettings(settings);
        result.className = "network-tool-result loading";
        result.textContent = "配置已保存，正在测试核心交换机连接…";
        const connection = await testDhcpConnection();
        result.className = `network-tool-result ${connection.privileged ? "good" : "warn"}`;
        result.textContent = `核心 IP 和 Telnet 信息已保存。${connection.message} · ${connection.host}:${connection.port}`;
      } catch (error) {
        result.className = "network-tool-result bad";
        result.textContent = settingsSaved
          ? `配置已保存，但连接测试失败：${error.message || "未知错误"}`
          : `保存失败：${error.message || "未知错误"}`;
      } finally {
        if (button) button.disabled = false;
      }
    }

    function renderConfigResult(payload) {
      const result = document.getElementById("controlConfigResult");
      if (!result) return;
      if (!payload || (payload.passive && !(payload.issues && payload.issues.length))) {
        result.innerHTML = `
          <div class="control-apply-next">
            <strong>配置流程</strong>
            <span>先点“验证”，确认无误后点“保存”或“应用配置”。</span>
          </div>
        `;
        return;
      }
      if (payload.pending) {
        result.innerHTML = `
          <div class="control-apply-next pending">
            <strong>正在${escapeHtml(payload.pendingLabel || "处理")}…</strong>
            <span>${escapeHtml(payload.pendingNote || "请稍候，不要重复点击或刷新页面。")}</span>
          </div>
        `;
        return;
      }
      if (!payload.ok && payload.error) {
        result.innerHTML = `
          <div class="control-apply-next bad">
            <strong>${escapeHtml(payload.errorTitle || "操作失败")}</strong>
            <span>${escapeHtml(payload.error)}</span>
          </div>
          ${payload.applyOutput ? `<pre class="control-apply-log">${escapeHtml(payload.applyOutput)}</pre>` : ""}
        `;
        return;
      }
      const issues = payload.issues || [];
      const issuesHtml = issues.map((item) => controlItemHtml({
        section: item.path || "配置",
        label: item.message || "配置项",
        level: item.level || "info",
        value: (item.level || "info").toUpperCase(),
        note: ""
      })).join("");
      let headline;
      if (payload.action === "rollback" && payload.applied) {
        headline = `
          <div class="control-apply-next good">
            <strong>↩ 回滚并应用完成</strong>
            <span>配置与 .env 已恢复到同一个历史版本，相关服务已重新验证。</span>
          </div>`;
      } else if (payload.applied) {
        headline = `
          <div class="control-apply-next good">
            <strong>🚀 应用完成</strong>
            <span>配置已写入 .env，相关容器已重启生效。</span>
          </div>`;
      } else if (payload.needsRedeploy) {
        headline = `
          <div class="control-apply-next warn">
            <strong>已保存，待应用</strong>
            <span>.env 已更新；点“应用配置”重启相关容器后才会生效。</span>
          </div>`;
      } else if (payload.action === "save") {
        headline = `
          <div class="control-apply-next good">
            <strong>💾 已保存</strong>
            <span>event-config.yml 已保存。点“应用配置”生成 .env 并让服务重启生效。</span>
          </div>`;
      } else if (payload.action === "rollback") {
        headline = `
          <div class="control-apply-next warn">
            <strong>↩ 已恢复文件，等待部署</strong>
            <span>配置与 .env 已成对恢复；当前环境关闭了自动应用，需要手动部署。</span>
          </div>`;
      } else if (issues.length) {
        headline = "";
      } else {
        headline = `
          <div class="control-apply-next good">
            <strong>✅ 验证通过</strong>
            <span>配置无误，可点“保存”或“应用配置”。</span>
          </div>`;
      }
      result.innerHTML = `${issuesHtml}${headline}`;
    }

    function configInput(path, label, options = {}) {
      const value = configPathGet(lastEditableConfig || {}, path);
      const id = `cfg-${path.replace(/[^a-z0-9]+/gi, "-")}`;
      const common = `id="${escapeHtml(id)}" data-config-path="${escapeHtml(path)}"${options.number ? ' data-config-number="1"' : ""}`;
      const fieldClasses = ["config-field"];
      if (options.compact) fieldClasses.push("config-field-compact");
      if (options.type === "checkbox") {
        const classes = ["config-field", "config-field-check"];
        if (options.compactCheck) classes.push("config-field-check-inline");
        return `
          <label class="${classes.join(" ")}" for="${escapeHtml(id)}">
            <input ${common} type="checkbox"${value ? " checked" : ""} />
            <span>${escapeHtml(label)}</span>
          </label>
        `;
      }
      if (options.type === "select") {
        return `
          <label class="${fieldClasses.join(" ")}" for="${escapeHtml(id)}">
            <span>${escapeHtml(label)}</span>
            <select ${common}>
              ${(options.choices || []).map((item) => `<option value="${escapeHtml(item.value)}"${String(value || "") === String(item.value) ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
            </select>
          </label>
        `;
      }
      if (options.type === "textarea") {
        const textareaClasses = fieldClasses.slice();
        if (options.wide || !options.compact) textareaClasses.push("config-field-wide");
        return `
          <label class="${textareaClasses.join(" ")}" for="${escapeHtml(id)}">
            <span>${escapeHtml(label)}</span>
            <textarea ${common} rows="${options.rows || 2}" placeholder="${escapeHtml(options.placeholder || "")}">${escapeHtml(csvText(value))}</textarea>
          </label>
        `;
      }
      return `
        <label class="${fieldClasses.join(" ")}" for="${escapeHtml(id)}">
          <span>${escapeHtml(label)}</span>
          <input ${common} type="${escapeHtml(options.inputType || (options.number ? "number" : "text"))}" value="${escapeHtml(configScalar(value))}" placeholder="${escapeHtml(options.placeholder || "")}" />
        </label>
      `;
    }

    function teamOrderConfigMarkup() {
      const layoutId = String((lastEditableConfig.event || {}).default_layout || "");
      const page = pages.find((item) => item.id === layoutId);
      const configurable = Array.isArray(teamLayouts.configurableLayoutIds)
        && teamLayouts.configurableLayoutIds.includes(layoutId);
      if (!page || !configurable || typeof teamLayouts.teamOrderForPage !== "function") return "";
      const order = teamLayouts.teamOrderForPage(page, lastEditableConfig.event.team_orders);
      const groups = Array.isArray(page.groups) ? page.groups : [page.teams || []];
      let slotIndex = 0;
      return `
        <div class="team-order-editor" data-team-order-layout="${escapeHtml(layoutId)}">
          <div class="team-order-heading">
            <div>
              <strong>队号位置</strong>
              <span>按舞台从上到下、每排从左到右排列；选择重复队号时会自动互换。</span>
            </div>
            <button type="button" data-team-order-reset="${escapeHtml(layoutId)}">恢复默认顺序</button>
          </div>
          <div class="team-order-stage">
            ${groups.map((group, groupIndex) => {
              const sideSize = Math.ceil(group.length / 2);
              const rowLabels = groups.length === 3 ? ["上层", "中层", "下层"] : ["上层", "下层"];
              const rowLabel = rowLabels[groupIndex] || `第 ${groupIndex + 1} 层`;
              return `
              <div class="team-order-row" style="--team-order-side-count:${sideSize}">
                ${group.map((_team, groupSlotIndex) => {
                  const currentIndex = slotIndex;
                  const selectedTeam = order[slotIndex];
                  const side = groupSlotIndex < sideSize ? "左侧" : "右侧";
                  const sideSlot = groupSlotIndex < sideSize ? groupSlotIndex + 1 : groupSlotIndex - sideSize + 1;
                  slotIndex += 1;
                  return `
                    <label${groupSlotIndex === sideSize ? ` class="team-order-side-start" style="grid-column-start:${sideSize + 2}"` : ""}>
                      <span>${rowLabel} · ${side}第 ${sideSlot} 位</span>
                      <select data-team-order-slot="${currentIndex}" data-team-order-previous="${selectedTeam}">
                        ${(page.teams || []).map((team) => `<option value="${team}"${team === selectedTeam ? " selected" : ""}>第 ${team} 队</option>`).join("")}
                      </select>
                    </label>
                  `;
                }).join("")}
              </div>
            `;
            }).join("")}
          </div>
        </div>
      `;
    }

    function configListRows(name, rows, columns) {
      const addLabels = {
        stage_switches: "赛事交换机",
        access_switches: "普通接入交换机",
        switches: "交换机",
        servers: "服务器",
        isp: "ISP"
      };
      const supportsRange = name === "stage_switches" || name === "access_switches";
      return `
        <div class="config-list" data-config-list="${escapeHtml(name)}">
          ${supportsRange ? `
            <div class="config-range-row">
              <input type="text" data-config-range-input="${escapeHtml(name)}" placeholder="范围或多个 IP" />
              <button type="button" data-config-add-range="${escapeHtml(name)}">添加范围</button>
            </div>
          ` : ""}
          ${rows.map((row, index) => `
            <div class="config-list-row" data-index="${index}">
              ${columns.map((column) => `
                <label>
                  <span>${escapeHtml(column.label)}</span>
                  <input data-config-key="${escapeHtml(column.key)}"${column.number ? ' data-config-number="1"' : ""} type="${column.number ? "number" : "text"}" value="${escapeHtml(configScalar(row[column.key]))}" placeholder="${escapeHtml(column.placeholder || "")}" />
                </label>
              `).join("")}
              <button type="button" data-config-remove="${escapeHtml(name)}" data-index="${index}">删除</button>
            </div>
          `).join("")}
          <button class="config-add-row" type="button" data-config-add="${escapeHtml(name)}">添加${escapeHtml(addLabels[name] || "条目")}</button>
        </div>
      `;
    }

    function controlDhcpSettingsMarkup() {
      return `
        <div class="config-private-section" id="core-telnet">
          <div class="network-tool-heading">
            <div>
              <h4>核心交换机 Telnet</h4>
              <p>用于只读 DHCP 查询；密码单独保存在本机，不随赛事配置导出。</p>
            </div>
            <span class="network-tool-badge">只读连接</span>
          </div>
          <div class="network-tool-grid telnet-settings-grid" id="controlDhcpSettingsForm">
            <label>Telnet 端口
              <input id="controlDhcpPort" type="number" min="1" max="65535" value="23" />
            </label>
            <label>用户名
              <input id="controlDhcpUsername" type="text" autocomplete="off" placeholder="按交换机实际配置填写" />
            </label>
            <label>登录密码
              <input id="controlDhcpPassword" type="password" autocomplete="new-password" placeholder="输入后保存；留空保留原密码" />
            </label>
            <label>Enable 密码
              <input id="controlDhcpEnablePassword" type="password" autocomplete="new-password" placeholder="没有可留空；留空保留原密码" />
            </label>
          </div>
          <div class="network-tool-actions">
            <button type="button" id="controlDhcpSaveTest">保存核心配置并测试</button>
            <span id="controlDhcpSavedState">等待读取配置</span>
          </div>
          <div class="network-tool-result" id="controlDhcpSettingsResult" hidden></div>
        </div>
      `;
    }

    function renderControlConfigForm(configValue) {
      const form = document.getElementById("controlConfigForm");
      if (!form) return;
      const telnetDraft = form.dataset.telnetDirty ? {
        username: (document.getElementById("controlDhcpUsername") || {}).value || "",
        password: (document.getElementById("controlDhcpPassword") || {}).value || "",
        enablePassword: (document.getElementById("controlDhcpEnablePassword") || {}).value || "",
        port: (document.getElementById("controlDhcpPort") || {}).value || "23"
      } : null;
      const matchPages = pages.filter((item) => item.kind);
      lastEditableConfig = controlConfigDefaults(configValue);
      form.innerHTML = `
        <section class="config-section">
          <h3>基础</h3>
          <div class="config-fields">
            ${configInput("event.name", "赛事名称", { placeholder: "可留空" })}
            ${configInput("event.default_layout", "默认赛制", { type: "select", choices: matchPages.map((item) => ({ value: item.id, label: item.label })) })}
          </div>
          ${teamOrderConfigMarkup()}
        </section>
        <section class="config-section">
          <h3>网络 / SNMP</h3>
          <div class="config-fields">
            ${configInput("snmp.community", "SNMP Community")}
            ${configInput("networks.player_vlan", "选手 VLAN", { number: true })}
            ${configInput("networks.wireless_vlan", "无线 VLAN", { number: true })}
            ${configInput("networks.player_subnets", "选手网段", { type: "textarea", compact: true, rows: 1, placeholder: "192.168.40.0/24" })}
            ${configInput("networks.wireless_subnets", "无线网段", { type: "textarea", compact: true, rows: 1, placeholder: "192.168.41.0/24" })}
            ${configInput("networks.player_gateways", "选手网关（可选）", { type: "textarea", compact: true, rows: 1, placeholder: "留空默认用核心交换机 IP" })}
            ${configInput("networks.switch_management_ranges", "普通交换机自动发现范围", { type: "textarea", compact: true, rows: 1, placeholder: "例如 192.168.10.1-100 或 192.168.10.0/24；只进入通用监控和拓扑，不参与赛事座位识别" })}
            ${configInput("networks.firewall_management_ranges", "防火墙管理网段", { type: "textarea", compact: true, rows: 1, placeholder: "默认 192.168.9.0/24；支持范围或单 IP" })}
          </div>
        </section>
        <section class="config-section">
          <h3>核心/防火墙</h3>
          <p class="config-section-note">防火墙 IP 同时用于 Ping 和 WAN 流量 SNMP；HA 物理机填物理防火墙 SNMP IP 后，单机会独立采集并有离线告警。</p>
          <div class="config-fields">
            ${configInput("devices.core.ip", "核心 IP")}
            ${configInput("devices.firewall.ip", "防火墙 IP", { type: "textarea", compact: true, rows: 1, placeholder: "可留空；多台逗号或换行分隔" })}
            ${configInput("devices.firewall.name", "防火墙名称（可选）", { placeholder: "大屏/拓扑显示名；留空用设备 SNMP sysName" })}
            ${configInput("devices.firewall.unit_snmp", "物理防火墙 SNMP IP", { type: "textarea", compact: true, rows: 1, placeholder: "两台物理防火墙，逗号或换行分隔" })}
          </div>
          ${controlDhcpSettingsMarkup()}
        </section>
        <div class="config-section-pair">
          <section class="config-section">
            <h3>赛事交换机（赛事项目必填）</h3>
            <p class="config-section-note">这里只填本项目承载选手电脑的交换机，例如 192.168.10.45、192.168.10.46。它们组成赛事白名单，用于座位识别和选手监控；普通管理网段里的其它交换机不会混入赛事控制台。</p>
            ${configListRows("stage_switches", lastEditableConfig.devices.stage_switches, [
              { key: "name", label: "名称", placeholder: "可留空，默认用 SNMP hostname" },
              { key: "ip", label: "管理地址", placeholder: "赛事交换机 IP" }
            ])}
          </section>
          <section class="config-section">
            <h3>固定普通交换机（选填）</h3>
            <p class="config-section-note">一般留空，系统会从“普通交换机自动发现范围”识别其它接入交换机。这里只在需要固定显示名称时填写；这些设备只进入通用大屏、拓扑和 LibreNMS，不参与选手座位识别。</p>
            ${configListRows("access_switches", lastEditableConfig.devices.access_switches, [
              { key: "name", label: "名称", placeholder: "可留空，默认用 SNMP hostname" },
              { key: "ip", label: "管理地址", placeholder: "可留空" }
            ])}
          </section>
        </div>
        <section class="config-section">
          <h3>服务器</h3>
          ${configListRows("servers", lastEditableConfig.devices.servers, [
            { key: "name", label: "名称", placeholder: "可留空" },
            { key: "ip", label: "地址", placeholder: "可留空" }
          ])}
        </section>
        <section class="config-section">
          <h3>ISP</h3>
          <p class="config-section-note">自动发现会从防火墙 SNMP 识别 WAN 接口，并从路由表发现网关。每条线路只需填写与防火墙一致的 WAN 口名/别名和带宽；不再要求公网 IP 或网关地址。对称线路填一个 Mbps 数值；不对称线路固定按“下载/上传”填写，例如 1000/100。</p>
          <div class="config-fields">
            ${configInput("isp.auto_discovery", "自动发现 ISP", { type: "checkbox", compactCheck: true })}
            ${configInput("isp.max_bandwidth_mbps", "默认带宽（下载/上传 Mbps）", { placeholder: "例如 1000 或 1000/100；留空默认 1000" })}
            ${configInput("isp.wan_if_filter", "WAN 口识别关键词", { placeholder: "telecom,telcom,unicom,isp,WAN" })}
          </div>
          ${configListRows("isp", lastEditableConfig.isp.links, [
            { key: "name", label: "WAN 口名/别名", placeholder: "例如 telecom、eth1 或电信" },
            { key: "bandwidth_mbps", label: "单线带宽（下载/上传 Mbps）", placeholder: "例如 1000 或 1000/100" }
          ])}
        </section>
        <section class="config-section">
          <h3>UniFi</h3>
          <div class="config-fields">
            ${configInput("unifi.enabled", "启用 UniFi", { type: "checkbox" })}
            ${configInput("unifi.controller_url", "UniFi 地址", { placeholder: "https://控制器IP" })}
            ${configInput("unifi.user", "UniFi 用户")}
            ${configInput("unifi.password", "UniFi 密码", { inputType: "password", placeholder: "留空则保留 .env 现有值" })}
            ${configInput("unifi.sites", "UniFi Sites", { placeholder: "all" })}
            ${configInput("unifi.verify_ssl", "校验 UniFi 证书", { type: "checkbox" })}
          </div>
        </section>
        <section class="config-section">
          <h3>告警</h3>
          <p class="config-section-note">所有监控可共用同一个飞书应用，但每台物理监控填写各自的“赛事名称”和“告警及巡检群名称”。现有长连接权限可继续使用；多套物理监控共用应用并要求严格按群处理时，再增加 im:chat、im:message:readonly 和 im:message.group_msg。</p>
          <div class="config-fields">
            ${configInput("alerts.feishu_robot_token", "飞书机器人 Token")}
            ${configInput("alerts.feishu_app_id", "飞书应用 App ID", { placeholder: "cli_ 开头" })}
            ${configInput("alerts.feishu_app_secret", "飞书应用 App Secret", { inputType: "password" })}
            ${configInput("alerts.feishu_chat_id", "告警及巡检群名称或 Chat ID", { placeholder: "唯一群名，或 oc_ 开头的 Chat ID" })}
            ${configInput("alerts.gateway_macs", "关键网关 MAC（逗号分隔）", { placeholder: "例如：0000.5e00.0101,0000.5e00.0201" })}
            ${configInput("alerts.gateway_uplink_ports", "网关正常上联接口（逗号分隔）", { placeholder: "例如：Po1,Po10" })}
            ${configInput("alerts.mac_flap_window_seconds", "MAC 漂移统计窗口（秒）", { number: true })}
            ${configInput("alerts.mac_flap_threshold", "普通 MAC 告警次数", { number: true })}
            ${configInput("alerts.cpu_alert_percent", "交换机 CPU 告警阈值（%）", { number: true })}
            ${configInput("alerts.memory_alert_percent", "交换机内存告警阈值（%）", { number: true })}
          </div>
          <p class="config-section-note">关键网关 MAC 在正常上联与其他接口间移动时立即告警；普通 MAC 默认 60 秒内出现 3 次才告警。Cisco 日志不提供可靠的原/新方向，因此未配置正常上联时只显示两个涉及接口。CPU 达到阈值持续 5 分钟才告警，内存持续 10 分钟才告警；分别低于阈值 10% 并稳定 2 分钟后恢复。默认 70% / 80%，40% 不会告警。</p>
        </section>
        <section class="config-section">
          <h3>安全</h3>
          <div class="config-fields">
            ${configInput("security.grafana_anonymous", "Grafana 匿名访问", { type: "checkbox" })}
          </div>
        </section>
      `;
      if (telnetDraft) {
        document.getElementById("controlDhcpUsername").value = telnetDraft.username;
        document.getElementById("controlDhcpPassword").value = telnetDraft.password;
        document.getElementById("controlDhcpEnablePassword").value = telnetDraft.enablePassword;
        document.getElementById("controlDhcpPort").value = telnetDraft.port;
      }
      if (lastDhcpSettings) renderControlDhcpSettings(lastDhcpSettings);
      if (window.location.hash === "#core-telnet") {
        window.requestAnimationFrame(() => {
          const target = document.getElementById("core-telnet");
          if (target) target.scrollIntoView({ block: "center" });
        });
      }
    }

    function collectControlConfigForm() {
      const form = document.getElementById("controlConfigForm");
      const value = controlConfigDefaults(lastEditableConfig);
      if (!form) return value;
      form.querySelectorAll("[data-config-path]").forEach((input) => {
        let nextValue;
        if (input.type === "checkbox") {
          nextValue = input.checked;
        } else if (input.tagName === "TEXTAREA") {
          nextValue = splitConfigList(input.value);
        } else if (input.dataset.configNumber) {
          nextValue = input.value === "" ? "" : Number(input.value);
        } else {
          nextValue = input.value.trim();
        }
        configPathSet(value, input.dataset.configPath, nextValue);
      });
      const listMappings = {
        stage_switches: ["devices", "stage_switches"],
        access_switches: ["devices", "access_switches"],
        servers: ["devices", "servers"],
        isp: ["isp", "links"]
      };
      Object.entries(listMappings).forEach(([name, path]) => {
        const list = form.querySelector(`[data-config-list="${name}"]`);
        const rows = [];
        if (list) {
          list.querySelectorAll(".config-list-row").forEach((row) => {
            const item = {};
            row.querySelectorAll("[data-config-key]").forEach((input) => {
              item[input.dataset.configKey] = input.dataset.configNumber
                ? (input.value === "" ? "" : Number(input.value))
                : input.value.trim();
            });
            if (Object.values(item).some((entry) => String(entry || "").trim())) rows.push(item);
          });
        }
        value[path[0]][path[1]] = rows;
      });
      const teamOrderEditor = form.querySelector("[data-team-order-layout]");
      if (teamOrderEditor) {
        const layoutId = teamOrderEditor.dataset.teamOrderLayout;
        const order = [...teamOrderEditor.querySelectorAll("[data-team-order-slot]")]
          .sort((left, right) => Number(left.dataset.teamOrderSlot) - Number(right.dataset.teamOrderSlot))
          .map((select) => Number(select.value));
        if (!value.event.team_orders || typeof value.event.team_orders !== "object" || Array.isArray(value.event.team_orders)) {
          value.event.team_orders = {};
        }
        value.event.team_orders[layoutId] = order;
      }
      if (value.devices) {
        value.devices.switches = [];
      }
      lastEditableConfig = value;
      return value;
    }

    function renderConfigEditor(platformConfig) {
      const form = document.getElementById("controlConfigForm");
      if (!form) return;
      if (platformConfig && platformConfig.ok && !form.dataset.dirty && !form.dataset.telnetDirty) {
        renderControlConfigForm(platformConfig.config || {});
      }
      const schemaBlocked = Boolean(platformConfig && platformConfig.configTooNew);
      ["controlConfigSave", "controlConfigApply", "controlConfigRollback", "controlConfigImport"].forEach((id) => {
        const button = document.getElementById(id);
        if (button) {
          button.disabled = schemaBlocked;
          button.title = schemaBlocked ? "配置版本高于当前软件，请先升级平台" : "";
        }
      });
      // Once the operator has run 验证/保存/应用配置, keep that result on screen --
      // don't let the periodic refresh overwrite it (that made the apply error
      // vanish into "验证通过" after a few seconds).
      if (configResultSticky) return;
      if (platformConfig && !platformConfig.ok) {
        renderConfigResult(platformConfig);
      } else if (platformConfig && platformConfig.ok) {
        renderConfigResult({ ok: true, passive: true, issues: platformConfig.issues || [] });
      }
    }

    const CONFIG_ACTION_LABELS = { validate: "验证", save: "保存", apply: "应用配置", rollback: "回滚" };

    function setConfigButtonsBusy(busy) {
      const schemaBlocked = Boolean(lastPlatformConfig && lastPlatformConfig.configTooNew);
      ["controlConfigValidate", "controlConfigSave", "controlConfigApply", "controlConfigRollback", "controlConfigImport"].forEach((id) => {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = busy || (schemaBlocked && id !== "controlConfigValidate");
      });
    }

    function configOperationId(action) {
      const random = Math.random().toString(36).slice(2, 10);
      return `web-${action}-${Date.now()}-${random}`;
    }

    async function runConfigAction(action) {
      const form = document.getElementById("controlConfigForm");
      const label = CONFIG_ACTION_LABELS[action] || "处理";
      const configPayload = collectControlConfigForm();
      const payload = { text: JSON.stringify(configPayload, null, 2), actor: "web", note: action };
      const operationId = (action === "apply" || action === "rollback") ? configOperationId(action) : "";
      if (operationId) payload.operationId = operationId;
      configResultSticky = true;
      if (action === "apply") applyInProgress = true;
      setConfigButtonsBusy(true);
      renderConfigResult({
        pending: true,
        pendingLabel: action === "apply"
          ? "应用配置，重启服务中（页面可能短暂断开约 10-20 秒，请勿刷新或关闭）"
          : label
      });
      try {
        let result;
        if (action === "validate") {
          result = await postPlatform("/config/validate", payload);
        } else if (action === "save") {
          result = await postPlatform("/config/save", payload);
        } else if (action === "apply") {
          result = await postPlatform("/config/apply", payload, { timeoutMs: APPLY_REQUEST_TIMEOUT_MS });
        } else if (action === "rollback") {
          result = await postPlatform("/config/rollback", { actor: "web", note: "rollback from control", operationId }, { timeoutMs: APPLY_REQUEST_TIMEOUT_MS });
        }
        result.action = action;
        lastPlatformConfig = result;
        const shouldReloadSavedConfig = result && result.ok && action !== "validate";
        if (shouldReloadSavedConfig && result.config && form) {
          delete form.dataset.dirty;
          renderControlConfigForm(result.config);
        } else if (form) {
          form.dataset.dirty = "1";
        }
        renderConfigResult(result);
        if (shouldReloadSavedConfig) {
          applyInProgress = false;
          onRefresh();
        }
      } catch (error) {
        if (action === "apply" || action === "rollback") {
          renderConfigResult({ pending: true, pendingLabel: "服务重启中，正在核对任务结果" });
          const recovered = await waitForApplyRecovery(operationId, {
            fetchConfig: fetchPlatformConfig,
            fetchStatus: fetchApplyStatus
          });
          const recoveryPayload = applyRecoveryRenderPayload(recovered, action);
          if (["succeeded", "pending", "failed"].includes(recovered.outcome)) {
            const recoveredConfig = recovered.config;
            if (recoveredConfig && recoveredConfig.ok) {
              lastPlatformConfig = recoveredConfig;
            }
            if (form && recoveredConfig && recoveredConfig.ok) {
              delete form.dataset.dirty;
              if (recoveredConfig.config) renderControlConfigForm(recoveredConfig.config);
            }
            renderConfigResult(recoveryPayload);
            applyInProgress = false;
            onRefresh();
          } else {
            // A durable task that still says running is not an apply failure.
            // Unknown is reserved for a task record that cannot be recovered.
            renderConfigResult(recoveryPayload);
          }
        } else {
          renderConfigResult({ ok: false, errorTitle: `${label}失败`, error: error.message || "配置操作失败" });
        }
      } finally {
        applyInProgress = false;
        setConfigButtonsBusy(false);
      }
    }

    function importControlConfigFile() {
      const input = document.getElementById("controlConfigImportFile");
      if (input) input.click();
    }

    function bindConfigImportFile() {
      const fileInput = document.getElementById("controlConfigImportFile");
      const form = document.getElementById("controlConfigForm");
      if (!fileInput || !form || fileInput.dataset.bound) return;
      fileInput.addEventListener("change", async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const text = await file.text();
        fileInput.value = "";
        // Reject compressed archives before treating their binary content as YAML.
        if (/\.zip$/i.test(file.name) || text.slice(0, 2) === "PK") {
          renderConfigResult({
            ok: false,
            errorTitle: "不支持导入压缩包",
            error: "请选择 event-config.yml 配置文件。"
          });
          configResultSticky = true;
          return;
        }
        try {
          const result = await postPlatform("/config/validate", { text, actor: "web", note: "import" });
          lastPlatformConfig = result;
          if (result && result.ok && !result.configTooNew && result.config) {
            renderControlConfigForm(result.config);
            form.dataset.dirty = "1";
          }
          renderConfigResult(result);
        } catch (error) {
          renderConfigResult({ ok: false, error: error.message || "导入失败" });
        }
      });
      fileInput.dataset.bound = "1";
    }

    function bind() {
      const configForm = document.getElementById("controlConfigForm");
      if (configForm && !configForm.dataset.bound) {
        const markDirty = (event) => {
          if (event.target.closest("#controlDhcpSettingsForm")) configForm.dataset.telnetDirty = "1";
          else configForm.dataset.dirty = "1";
        };
        configForm.addEventListener("input", markDirty);
        configForm.addEventListener("change", (event) => {
          markDirty(event);
          const layoutSelect = event.target.closest('[data-config-path="event.default_layout"]');
          if (layoutSelect) {
            const next = collectControlConfigForm();
            renderControlConfigForm(next);
            configForm.dataset.dirty = "1";
            return;
          }
          const teamSelect = event.target.closest("[data-team-order-slot]");
          if (!teamSelect) return;
          const previous = String(teamSelect.dataset.teamOrderPrevious || "");
          const selected = String(teamSelect.value || "");
          const duplicate = [...configForm.querySelectorAll("[data-team-order-slot]")]
            .find((input) => input !== teamSelect && String(input.value) === selected);
          if (duplicate) {
            duplicate.value = previous;
            duplicate.dataset.teamOrderPrevious = previous;
          }
          teamSelect.dataset.teamOrderPrevious = selected;
          collectControlConfigForm();
        });
        // Browsers increment focused number inputs when the mouse wheel moves.
        // Remove focus before the native wheel action so scrolling the long
        // configuration form cannot silently change VLAN or bandwidth values.
        configForm.addEventListener("wheel", (event) => {
          const input = event.target instanceof HTMLInputElement ? event.target : null;
          if (input && input.type === "number" && document.activeElement === input) input.blur();
        }, { passive: true });
        configForm.addEventListener("click", (event) => {
          const dhcpSaveButton = event.target.closest("#controlDhcpSaveTest");
          if (dhcpSaveButton) {
            saveAndTestControlDhcpSettings(event);
            return;
          }
          const teamOrderReset = event.target.closest("[data-team-order-reset]");
          if (teamOrderReset) {
            const next = collectControlConfigForm();
            const layoutId = teamOrderReset.dataset.teamOrderReset;
            const page = pages.find((item) => item.id === layoutId);
            if (page && typeof teamLayouts.defaultTeamOrder === "function") {
              next.event.team_orders[layoutId] = teamLayouts.defaultTeamOrder(page);
              renderControlConfigForm(next);
              configForm.dataset.dirty = "1";
            }
            return;
          }
          const addButton = event.target.closest("[data-config-add]");
          const rangeButton = event.target.closest("[data-config-add-range]");
          const removeButton = event.target.closest("[data-config-remove]");
          if (!addButton && !rangeButton && !removeButton) return;
          const next = collectControlConfigForm();
          if (addButton) {
            const listName = addButton.dataset.configAdd;
            if (listName === "stage_switches") next.devices.stage_switches.push({ ip: "" });
            if (listName === "access_switches") next.devices.access_switches.push({ ip: "" });
            if (listName === "servers") next.devices.servers.push({ name: "", ip: "" });
            if (listName === "isp") next.isp.links.push({ name: "", ping: "", ip: "", bandwidth_mbps: "" });
          }
          if (rangeButton) {
            const listName = rangeButton.dataset.configAddRange;
            const input = configForm.querySelector(`[data-config-range-input="${listName}"]`);
            const values = expandIpRangeText(input ? input.value : "");
            const target = listName === "stage_switches" ? next.devices.stage_switches : next.devices.access_switches;
            const known = new Set(target.map((item) => String(item.ip || "").trim()).filter(Boolean));
            values.forEach((ip) => {
              if (!known.has(ip)) {
                target.push({ ip });
                known.add(ip);
              }
            });
          }
          if (removeButton) {
            const listName = removeButton.dataset.configRemove;
            const index = Number(removeButton.dataset.index);
            if (listName === "stage_switches") next.devices.stage_switches.splice(index, 1);
            if (listName === "access_switches") next.devices.access_switches.splice(index, 1);
            if (listName === "servers") next.devices.servers.splice(index, 1);
            if (listName === "isp") next.isp.links.splice(index, 1);
          }
          renderControlConfigForm(next);
          configForm.dataset.dirty = "1";
        });
        configForm.dataset.bound = "1";
      }
      [
        ["controlConfigValidate", "validate"],
        ["controlConfigSave", "save"],
        ["controlConfigApply", "apply"],
        ["controlConfigRollback", "rollback"]
      ].forEach(([id, action]) => {
        const button = document.getElementById(id);
        if (button && !button.dataset.bound) {
          button.addEventListener("click", () => runConfigAction(action));
          button.dataset.bound = "1";
        }
      });
      const importBtn = document.getElementById("controlConfigImport");
      if (importBtn && !importBtn.dataset.bound) {
        importBtn.addEventListener("click", importControlConfigFile);
        importBtn.dataset.bound = "1";
      }
      bindConfigImportFile();
    }

    function render(platformConfig, dhcpSettings) {
      renderConfigEditor(platformConfig);
      renderControlDhcpSettings(dhcpSettings);
      lastPlatformConfig = platformConfig;
    }

    function isApplyInProgress() {
      return applyInProgress;
    }

    return { bind, render, isApplyInProgress };
  }

  const ns = { createConfigEditor };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSConfigEditor = ns;
  }
}());
