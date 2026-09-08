;(function () {
  'use strict';

  function createControlRefreshLifecycle() {
    let generation = 0;
    let sequence = 0;
    let committedSequence = 0;
    let active = false;

    function invalidate() {
      generation += 1;
      committedSequence = sequence;
    }

    function start() {
      invalidate();
      active = true;
    }

    function stop() {
      invalidate();
      active = false;
    }

    function isCurrent(token) {
      return active
        && token.generation === generation
        && token.sequence > committedSequence;
    }

    function commit(token) {
      if (!isCurrent(token)) return false;
      committedSequence = token.sequence;
      return true;
    }

    async function execute(task, onSuccess, onError) {
      if (!active) return false;
      const token = { generation, sequence: ++sequence };
      let result;
      try {
        result = await task(() => isCurrent(token));
      } catch (error) {
        if (!commit(token)) return false;
        try {
          onError(error);
        } catch (_renderError) {
          // Timer-triggered refreshes must not create unhandled rejections.
        }
        return false;
      }
      if (!commit(token)) return false;
      try {
        onSuccess(result);
        return true;
      } catch (error) {
        try {
          onError(error);
        } catch (_renderError) {
          // Keep refresh failures inside this controlled path.
        }
        return false;
      }
    }

    return { start, stop, invalidate, execute };
  }

  function createAuthController(dependencies) {
    const {
      document,
      fetchPlatformAuthStatus,
      loginPlatformAuth,
      logoutPlatformAuth,
      onAuthenticated,
      onLoggedOut
    } = dependencies;

    let lastControlAuth = null;
    let authGeneration = 0;
    let authSequence = 0;
    let committedAuthSequence = 0;
    let passwordCompositionActive = false;

    const passwordAsciiWarning = "密码仅支持英文半角字符，已移除不支持的字符。";

    function invalidate() {
      authGeneration += 1;
      committedAuthSequence = authSequence;
    }

    function beginAuthRequest(newLifecycle = false) {
      if (newLifecycle) invalidate();
      return { generation: authGeneration, sequence: ++authSequence };
    }

    function commitAuthRequest(token) {
      if (token.generation !== authGeneration || token.sequence <= committedAuthSequence) {
        return false;
      }
      committedAuthSequence = token.sequence;
      return true;
    }

    function setAuthMessage(message, level = "") {
      const element = document.getElementById("controlAuthMessage");
      if (!element) return;
      element.className = `auth-message ${level || ""}`.trim();
      element.textContent = message || "";
    }

    function passwordHasNonAscii(value) {
      return /[^\x00-\x7f]/.test(String(value));
    }

    function cleanPasswordInput(passwordInput) {
      if (!passwordInput) return false;
      const original = passwordInput.value;
      const cleaned = original.replace(/[^\x00-\x7f]/g, "");
      if (cleaned === original) return false;

      const selectionStart = Number.isInteger(passwordInput.selectionStart)
        ? passwordInput.selectionStart
        : null;
      const selectionEnd = Number.isInteger(passwordInput.selectionEnd)
        ? passwordInput.selectionEnd
        : null;
      passwordInput.value = cleaned;
      if (selectionStart !== null && selectionEnd !== null
          && typeof passwordInput.setSelectionRange === "function") {
        const cleanedStart = original.slice(0, selectionStart).replace(/[^\x00-\x7f]/g, "").length;
        const cleanedEnd = original.slice(0, selectionEnd).replace(/[^\x00-\x7f]/g, "").length;
        passwordInput.setSelectionRange(cleanedStart, cleanedEnd);
      }
      setAuthMessage(passwordAsciiWarning, "bad");
      return true;
    }

    function handlePasswordInput(event) {
      if (passwordCompositionActive || (event && event.isComposing)) return;
      cleanPasswordInput(event && event.currentTarget);
    }

    function handlePasswordCompositionStart() {
      passwordCompositionActive = true;
    }

    function handlePasswordCompositionEnd(event) {
      passwordCompositionActive = false;
      cleanPasswordInput(event && event.currentTarget);
    }

    function renderAuth(status) {
      const authPanel = document.getElementById("controlAuth");
      const shell = document.getElementById("controlShell");
      const loginForm = document.getElementById("controlLoginForm");
      const userInput = document.getElementById("controlLoginUser");
      const title = document.getElementById("controlAuthTitle");
      const hint = document.getElementById("controlAuthHint");
      const authenticated = status && status.authenticated;

      if (!authPanel || !shell) return true;
      if (authenticated) {
        authPanel.hidden = true;
        shell.hidden = false;
        setAuthMessage("");
        return true;
      }

      shell.hidden = true;
      authPanel.hidden = false;
      if (loginForm) loginForm.hidden = false;
      if (userInput && status && status.defaultUser && !userInput.value) userInput.value = status.defaultUser;
      if (title) title.textContent = "赛事控制台登录";
      if (hint) hint.textContent = "输入控制台账号密码后继续。";
      if (status && status.error) {
        setAuthMessage(status.error, "bad");
      } else {
        setAuthMessage("");
      }
      return false;
    }

    async function ensureAuthenticated() {
      const token = beginAuthRequest();
      const status = await fetchPlatformAuthStatus();
      if (!commitAuthRequest(token)) return null;
      // During a transient proxy outage (bigscreen restarting on 应用配置) the
      // auth probe fails with no HTTP status. If we were already authenticated,
      // hold the console rather than tearing it down to the login screen -- the
      // next poll will recover on its own.
      if (status && status.transient && lastControlAuth && lastControlAuth.authenticated) {
        return true;
      }
      lastControlAuth = status;
      return renderAuth(status);
    }

    async function submitLogin(event) {
      event.preventDefault();
      const token = beginAuthRequest(true);
      const passwordInput = document.getElementById("controlLoginPassword");
      if (passwordCompositionActive) {
        setAuthMessage("请先完成密码输入后再登录。", "bad");
        return;
      }
      const username = (document.getElementById("controlLoginUser") || {}).value || "";
      const password = passwordInput ? passwordInput.value : "";
      if (passwordHasNonAscii(password)) {
        cleanPasswordInput(passwordInput);
        return;
      }
      setAuthMessage("正在登录...");
      try {
        const status = await loginPlatformAuth(username.trim(), password);
        if (!commitAuthRequest(token)) return;
        lastControlAuth = status;
        if (passwordInput) passwordInput.value = "";
        renderAuth(lastControlAuth);
        if (lastControlAuth.authenticated) {
          onAuthenticated();
        }
      } catch (error) {
        if (!commitAuthRequest(token)) return;
        setAuthMessage(error.message || "登录失败", "bad");
      }
    }

    async function logout() {
      invalidate();
      lastControlAuth = { ok: true, enabled: true, authenticated: false };
      onLoggedOut();
      renderAuth(lastControlAuth);
      try {
        await logoutPlatformAuth();
      } catch (error) {
        // Logout is best effort; local UI should still return to the login screen.
      }
    }

    function bind() {
      const loginForm = document.getElementById("controlLoginForm");
      if (loginForm && !loginForm.dataset.bound) {
        loginForm.addEventListener("submit", submitLogin);
        loginForm.dataset.bound = "1";
      }
      const passwordInput = document.getElementById("controlLoginPassword");
      if (passwordInput && !passwordInput.dataset.asciiBound) {
        passwordInput.addEventListener("input", handlePasswordInput);
        passwordInput.addEventListener("compositionstart", handlePasswordCompositionStart);
        passwordInput.addEventListener("compositionend", handlePasswordCompositionEnd);
        passwordInput.dataset.asciiBound = "1";
      }
      const logoutBtn = document.getElementById("controlLogout");
      if (logoutBtn && !logoutBtn.dataset.bound) {
        logoutBtn.addEventListener("click", logout);
        logoutBtn.dataset.bound = "1";
      }
    }

    return { bind, ensureAuthenticated, invalidate };
  }

  const ns = { createAuthController, createControlRefreshLifecycle };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSAuthController = ns;
  }
}());
