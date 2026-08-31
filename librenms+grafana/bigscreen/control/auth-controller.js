;(function () {
  'use strict';

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

    function setAuthMessage(message, level = "") {
      const element = document.getElementById("controlAuthMessage");
      if (!element) return;
      element.className = `auth-message ${level || ""}`.trim();
      element.textContent = message || "";
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
      const status = await fetchPlatformAuthStatus();
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
      const username = (document.getElementById("controlLoginUser") || {}).value || "";
      const passwordInput = document.getElementById("controlLoginPassword");
      const password = passwordInput ? passwordInput.value : "";
      setAuthMessage("正在登录...");
      try {
        lastControlAuth = await loginPlatformAuth(username.trim(), password);
        if (passwordInput) passwordInput.value = "";
        renderAuth(lastControlAuth);
        if (lastControlAuth.authenticated) {
          onAuthenticated();
        }
      } catch (error) {
        setAuthMessage(error.message || "登录失败", "bad");
      }
    }

    async function logout() {
      try {
        await logoutPlatformAuth();
      } catch (error) {
        // Logout is best effort; local UI should still return to the login screen.
      }
      lastControlAuth = { ok: true, enabled: true, authenticated: false };
      onLoggedOut();
      renderAuth(lastControlAuth);
    }

    function bind() {
      const loginForm = document.getElementById("controlLoginForm");
      if (loginForm && !loginForm.dataset.bound) {
        loginForm.addEventListener("submit", submitLogin);
        loginForm.dataset.bound = "1";
      }
      const logoutBtn = document.getElementById("controlLogout");
      if (logoutBtn && !logoutBtn.dataset.bound) {
        logoutBtn.addEventListener("click", logout);
        logoutBtn.dataset.bound = "1";
      }
    }

    return { bind, ensureAuthenticated };
  }

  const ns = { createAuthController };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSAuthController = ns;
  }
}());
