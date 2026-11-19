;(function () {
  'use strict';

  const { groups } = window.BSPingCustomModel;
  const dns = window.BSPingCustomDNS;

  const errors = {
    custom_ping_auth_required: "会话已结束，请重新登录。",
    custom_ping_auth_disabled: "平台鉴权已关闭，自定义目标为只读。",
    custom_ping_auth_unavailable: "平台会话暂不可用。",
    custom_ping_admin_required: "需要管理员会话。",
    custom_ping_write_disabled: "平台已关闭自定义目标写入。",
    custom_ping_csrf_rejected: "安全令牌已失效，请重新确认登录状态。",
    custom_ping_ip_outside_allowed_cidrs: "IPv4 不在批准的地址范围内。",
    custom_ping_invalid_ipv4: "请输入合法的单播 IPv4 地址。",
    custom_ping_duplicate_ip: "该地址或域名已被监控目标占用。",
    custom_ping_invalid_address: "服务端拒绝该域名或私网授权，请核对格式。",
    custom_ping_capacity: "已达到目标数量上限。",
    custom_ping_idempotency_conflict: "请求标识与原操作不一致，请刷新清单。",
    custom_ping_not_found: "该目标已移除，请刷新清单。",
    custom_ping_revision_conflict: "目标已被更新，确认最新版本后再提交。",
    custom_ping_revision_required: "缺少目标版本，请重新查看目标。",
    custom_ping_inventory_unavailable: "现有监控库存暂不可确认。",
    custom_ping_commit_failed: "服务端保存失败，请核对后重试。",
    random_unavailable: "浏览器无法生成安全请求标识。",
    timeout: "请求超时，保存结果暂未确认。",
    network: "请求中断，保存结果暂未确认。",
    invalid_response: "服务端响应无效，保存结果暂未确认。"
  };

  function createManager({ byId, client, monitor, auth }) {
    const el = (name) => byId("Custom" + name);
    const dialog = el("Dialog");
    const form = el("Form");
    const login = el("Login");
    const name = el("Name");
    const ip = el("IP");
    const group = el("Group");
    const privateOptions = el("Private");
    const cidrs = el("CIDRs");
    const formFields = [name, ip, group, cidrs, el("Save")];
    let grantAddress = null;

    function updatePrivate() {
      const parsed = dns.addressInput(ip.value);
      const host = parsed?.kind === "dns" ? parsed.address : null;
      if (host !== grantAddress) {
        cidrs.value = "";
        privateOptions.open = false;
        grantAddress = host;
      }
      privateOptions.hidden = host === null;
      return parsed;
    }

    ip.addEventListener("input", updatePrivate);
    const rows = el("Rows");
    let view = { catalog: null, items: [], notice: "" };
    let origin = null;
    let accessRequest;
    let writeRequest;
    let token;
    let showEditor = false;
    let editing = null;
    let deleting = null;
    let retry = null;
    let conflict = null;
    let rowSignature = null;
    let loginBusy = false;
    let catalogCurrent = false;

    const open = () => dialog.open;
    const latest = (id) => view.catalog?.targets.find((row) => row.id === id);
    const canModify = () => !!token && !!view.catalog && catalogCurrent && !writeRequest && !retry && !conflict;
    const message = (error, fallback) => errors[error?.code] ||
      (error?.status === 503 ? "自定义目标服务暂不可用。" : fallback);

    function announce(text, focus = false) {
      el("Message").textContent = text;
      if (focus) el("Message").focus();
    }

    function button(label, action, id) {
      const element = document.createElement("button");
      element.type = "button";
      element.textContent = label;
      element.dataset.action = action;
      element.dataset.id = id;
      return element;
    }

    function render() {
      if (!open()) return;
      const list = view.catalog?.targets || [];
      const count = list.filter((row) => row.enabled).length;
      const publication = view.sync === "待发现" ? "已发布 · 待发现" : view.sync || "正在读取清单";
      el("Inventory").textContent = view.catalog
        ? count + " / 100 启用 · " + list.length + " / 300 总数 · " + publication + (view.notice ? " · " + view.notice : "")
        : view.notice || publication;
      const status = new Map(view.items.map((item) => [item.metric.custom_id, item]));
      const writable = canModify();
      const fingerprint = JSON.stringify([writable, !!token, list.map(({ id, name, ip, address, resolvedIp, group, enabled, revision }) =>
        [id, name, ip, address, resolvedIp, group, enabled, revision, status.get(id)?.sync, status.get(id)?.status])]);
      if (rowSignature !== fingerprint) {
        let focused = null;
        if (rows.contains(document.activeElement)) focused = { ...document.activeElement.dataset };
        rowSignature = fingerprint;
        rows.replaceChildren(...list.map((row) => {
          const line = document.createElement("li");
          const caption = document.createElement("span");
          const state = status.get(row.id);
          caption.textContent = [row.address || row.name, row.address ? row.name : row.ip,
            row.address && row.resolvedIp, row.group, row.enabled ? (state?.sync || "待发现") +
            " · " + (state?.status || "UNKNOWN") : "已停用"].filter(Boolean).join(" · ");
          line.append(caption);
          if (token) {
            const actions = document.createElement("div");
            actions.className = "ping-custom-actions";
            for (const [label, action] of [["编辑", "edit"], [row.enabled ? "停用" : "恢复", "toggle"], ["删除", "delete"]]) {
              const control = button(label, action, row.id);
              control.setAttribute("aria-label", label + " " + row.name);
              control.disabled = !writable;
              actions.append(control);
            }
            line.append(actions);
          }
          return line;
        }));
        if (focused) {
          const replacement = [...rows.querySelectorAll("button")].find((item) =>
            !item.disabled && item.dataset.id === focused.id && item.dataset.action === focused.action);
          (replacement || el("Message")).focus({ preventScroll: true });
        }
      }
      form.hidden = !token || !showEditor;
      for (const field of formFields) field.disabled = !writable;
      el("New").hidden = !token;
      el("New").disabled = !writable;
      el("Logout").hidden = !token;
      el("Confirm").hidden = !deleting;
      el("Delete").disabled = !writable;
      el("Retry").hidden = !retry;
      el("Retry").disabled = !!writeRequest || !token;
      el("Dismiss").hidden = !retry;
      el("Conflict").hidden = !conflict;
      el("Review").disabled = !token || !catalogCurrent;
    }

    async function verifyAccess({ keepMessage = false } = {}) {
      accessRequest?.abort();
      const request = new AbortController();
      accessRequest = request;
      token = null;
      login.hidden = true;
      render();
      try {
        const session = await auth.fetchPlatformAuthStatus();
        if (request.signal.aborted || !open()) return;
        if (session.enabled !== true || session.ok !== true) {
          throw new Error(session.enabled === false ? errors.custom_ping_auth_disabled : errors.custom_ping_auth_unavailable);
        }
        if (!session.authenticated) {
          login.hidden = false;
          if (!keepMessage) announce("登录管理员会话后可以修改目标。");
          return;
        }
        const result = await client.csrf(request.signal);
        if (request.signal.aborted || !open()) return;
        if (!/^[0-9a-f]{64}$/.test(result.csrfToken || "")) throw new Error(errors.invalid_response);
        token = result.csrfToken;
        if (!keepMessage) announce("");
        render();
        if (showEditor && catalogCurrent) name.focus();
      } catch (error) {
        if (!request.signal.aborted && open()) announce(message(error, error.message), true);
      } finally {
        if (accessRequest === request && open()) render();
      }
    }

    function beginEditor(row = null) {
      editing = row;
      showEditor = true;
      name.value = row?.name || "";
      ip.value = row?.address || row?.ip || "";
      grantAddress = row?.address || null;
      cidrs.value = row?.privateCidrs?.join(", ") || "";
      privateOptions.open = !!cidrs.value;
      updatePrivate();
      group.value = row?.group || groups[0];
      el("EditorTitle").textContent = row ? "编辑目标" : "添加目标";
      render();
      name.focus();
    }

    function openDialog(event, add) {
      origin = event.currentTarget;
      editing = null;
      showEditor = add;
      if (add) beginEditor();
      dialog.showModal();
      announce("正在检查会话…");
      render();
      void verifyAccess();
    }

    function stop() {
      accessRequest?.abort();
      writeRequest?.abort();
      accessRequest = writeRequest = null;
      token = null;
      retry = conflict = deleting = null;
      loginBusy = false;
      el("LoginSubmit").disabled = false;
      rowSignature = null;
      el("Pass").value = "";
      if (open()) dialog.close();
    }

    async function send(operation) {
      if (!token || writeRequest) return;
      const request = new AbortController();
      writeRequest = request;
      render();
      try {
        const receipt = await client.write(operation, token, request.signal);
        if (request.signal.aborted || !open()) return;
        monitor.saved(receipt);
        retry = null;
        if (operation.method === "POST" || operation.method === "PATCH" && showEditor) showEditor = false;
        deleting = null;
        announce("已保存。", true);
      } catch (error) {
        if (request.signal.aborted || !open()) return;
        if (error.status === 401 || error.code === "custom_ping_csrf_rejected" ||
            ["custom_ping_auth_disabled", "custom_ping_write_disabled", "custom_ping_admin_required",
              "custom_ping_auth_unavailable"].includes(error.code)) {
          token = null;
          retry = error.status === 401 || error.code === "custom_ping_csrf_rejected" ? operation : null;
          void verifyAccess({ keepMessage: true });
        } else if (error.status === 412 || error.status === 428) {
          retry = null;
          conflict = { operation };
          catalogCurrent = false;
          void monitor.refresh();
        } else if (error.status === 503 || error.status >= 500 || ["network", "timeout", "invalid_response"].includes(error.code)) {
          retry = operation;
        } else retry = null;
        announce(message(error, "操作失败，请检查输入后重试。"), true);
      } finally {
        if (writeRequest === request) {
          writeRequest = null;
          render();
        }
      }
    }

    function submit(method, row, payload) {
      if (!canModify()) return;
      try { void send(client.operation(method, row, payload)); }
      catch (error) { announce(message(error, "无法准备请求。"), true); }
    }

    el("Add").addEventListener("click", (event) => openDialog(event, true));
    el("Manage").addEventListener("click", (event) => openDialog(event, false));
    el("Close").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      if (open()) return;
      stop();
      if (origin?.isConnected && !origin.closest("[hidden]")) origin.focus();
    });
    el("New").addEventListener("click", () => beginEditor());
    el("EditorCancel").addEventListener("click", () => {
      if (writeRequest || retry || conflict) return;
      showEditor = false;
      editing = null;
      render();
      el("New").focus();
    });

    login.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (loginBusy || !open()) return;
      const request = accessRequest;
      loginBusy = true;
      el("LoginSubmit").disabled = true;
      const password = el("Pass").value;
      el("Pass").value = "";
      try {
        await auth.loginPlatformAuth(el("User").value, password);
        if (open() && !request?.signal.aborted) {
          loginBusy = false;
          el("LoginSubmit").disabled = false;
          await verifyAccess();
        }
      } catch (error) {
        if (open() && !request?.signal.aborted) announce(error.message || "登录失败。", true);
      } finally {
        if (!request?.signal.aborted) {
          loginBusy = false;
          el("LoginSubmit").disabled = false;
        }
      }
    });

    el("Logout").addEventListener("click", async () => {
      token = null;
      retry = conflict = deleting = null;
      accessRequest?.abort();
      writeRequest?.abort();
      writeRequest = null;
      render();
      const request = new AbortController();
      accessRequest = request;
      try {
        await auth.logoutPlatformAuth();
        if (open() && !request.signal.aborted) await verifyAccess();
      } catch {
        if (open() && !request.signal.aborted) announce("注销暂不可确认，请关闭窗口后重新检查会话。", true);
      }
    });

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!canModify()) return;
      const parsed = updatePrivate();
      if (!parsed) {
        announce("请输入合法的 IPv4 或 DNS 主机名。");
        ip.focus();
        return;
      }
      const networks = parsed.kind === "dns" ? dns.privateCidrs(cidrs.value) : [];
      if (!networks) {
        announce("请输入有效的 RFC1918 私网 CIDR，最多 16 项。");
        privateOptions.open = true;
        cidrs.focus();
        return;
      }
      const payload = dns.editPayload(name.value, parsed, group.value, networks);
      if (!payload) {
        announce("名称须为 1–80 个字符且最多 240 字节，不含控制字符。");
        name.focus();
        return;
      }
      submit(editing ? "PATCH" : "POST", editing, payload);
    });

    rows.addEventListener("click", (event) => {
      const control = event.target.closest("button[data-action]");
      if (!control || !canModify()) return;
      const row = latest(control.dataset.id);
      if (!row) return;
      if (control.dataset.action === "edit") beginEditor(row);
      if (control.dataset.action === "toggle") submit("PATCH", row, { enabled: !row.enabled });
      if (control.dataset.action === "delete") {
        deleting = row;
        el("DeleteText").textContent = "删除 “" + row.name + "”（" + (row.address || row.ip) + "）？已采集的历史仍可通过原链接查看。";
        render();
        el("CancelDelete").focus();
      }
    });
    el("CancelDelete").addEventListener("click", () => {
      if (writeRequest || retry || conflict) return;
      const id = deleting?.id;
      deleting = null;
      render();
      [...rows.querySelectorAll("button")].find((item) => item.dataset.id === id && item.dataset.action === "delete")?.focus();
    });
    el("Delete").addEventListener("click", () => { if (deleting) submit("DELETE", deleting); });
    el("Retry").addEventListener("click", () => { if (retry) void send(retry); });
    el("Dismiss").addEventListener("click", () => {
      if (writeRequest) return;
      retry = null;
      catalogCurrent = false;
      render();
      void monitor.refresh();
    });
    el("Review").addEventListener("click", () => {
      if (!catalogCurrent || !conflict) return;
      const changed = latest((editing || deleting)?.id);
      const hadSelection = !!(editing || deleting);
      conflict = deleting = null;
      if (editing) editing = changed || null;
      if (!changed && hadSelection) showEditor = false;
      announce(changed ? "已采用最新版本，请核对清单和保留的草稿后再次操作。" : "请核对最新清单后重新选择操作。", true);
      render();
    });

    return { stop, update(update) {
      view = update;
      catalogCurrent = !!view.catalog?.complete && !view.notice.startsWith("自定义清单");
      render();
    } };
  }

  window.BSPingCustomManager = { createManager };
})();
