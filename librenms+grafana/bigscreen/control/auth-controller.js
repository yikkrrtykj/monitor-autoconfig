;(function () {
  'use strict';

  function createAuthController(dependencies) {
    const {
      document,
      fetchPlatformAuthStatus,
      loginPlatformAuth,
      changePlatformPassword,
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
      const passwordForm = document.getElementById("controlPasswordForm");
      const userInput = document.getElementById("controlLoginUser");
      const title = document.getElementById("controlAuthTitle");
      const hint = document.getElementById("controlAuthHint");
      const authenticated = status && status.authenticated;
      const mustChange = authenticated && status.mustChangePassword;

      if (!authPanel || !shell) return true;
      if (authenticated && !mustChange) {
        authPanel.hidden = true;
        shell.hidden = false;
        setAuthMessage("");
        return true;
      }

      shell.hidden = true;
      authPanel.hidden = false;
      if (loginForm) loginForm.hidden = Boolean(authenticated);
      if (passwordForm) passwordForm.hidden = !mustChange;
      if (userInput && status && status.defaultUser && !userInput.value) userInput.value = status.defaultUser;
      if (title) title.textContent = mustChange ? "首次登录需要修改密码" : "赛事控制台登录";
      if (hint) {
        hint.textContent = mustChange
          ? "默认密码只能用于首次进入，请设置一个新的控制台密码。"
          : "输入控制台账号密码后继续。";
      }
      if (status && status.error) {
        setAuthMessage(status.error, "bad");
      } else if (mustChange) {
        setAuthMessage("新密码至少 10 位，并包含字母和数字。", "");
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
        if (lastControlAuth.authenticated && !lastControlAuth.mustChangePassword) {
          onAuthenticated();
        }
      } catch (error) {
        setAuthMessage(error.message || "登录失败", "bad");
      }
    }

    async function submitPasswordChange(event) {
      event.preventDefault();
      const currentInput = document.getElementById("controlCurrentPassword");
      const nextInput = document.getElementById("controlNewPassword");
      const confirmInput = document.getElementById("controlConfirmPassword");
      const currentPassword = currentInput ? currentInput.value : "";
      const newPassword = nextInput ? nextInput.value : "";
      const confirmPassword = confirmInput ? confirmInput.value : "";
      if (newPassword !== confirmPassword) {
        setAuthMessage("两次输入的新密码不一致", "bad");
        return;
      }
      setAuthMessage("正在修改密码...");
      try {
        lastControlAuth = await changePlatformPassword(currentPassword, newPassword, confirmPassword);
        [currentInput, nextInput, confirmInput].forEach((input) => { if (input) input.value = ""; });
        setAuthMessage("密码已修改", "good");
        renderAuth(lastControlAuth);
        onAuthenticated();
      } catch (error) {
        setAuthMessage(error.message || "修改密码失败", "bad");
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
      const passwordForm = document.getElementById("controlPasswordForm");
      if (passwordForm && !passwordForm.dataset.bound) {
        passwordForm.addEventListener("submit", submitPasswordChange);
        passwordForm.dataset.bound = "1";
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
