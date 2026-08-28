const assert = require('assert');
const authControllerModule = require('../bigscreen/control/auth-controller.js');

assert.deepStrictEqual(
  Object.keys(authControllerModule),
  ['createAuthController'],
  'the auth controller exposes only its dependency-injected factory'
);

class FakeElement {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.hidden = false;
    this.className = '';
    this._textContent = '';
    this.textHistory = [];
    this.value = '';
  }

  set textContent(value) {
    this._textContent = String(value);
    this.textHistory.push(this._textContent);
  }

  get textContent() {
    return this._textContent;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  dispatch(type) {
    const event = {
      type,
      target: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }
    };
    const pending = (this.listeners.get(type) || []).map((handler) => handler(event));
    return {
      event,
      promise: Promise.all(pending.filter((result) => result && typeof result.then === 'function'))
    };
  }
}

class FakeDocument {
  constructor(options = {}) {
    this.elements = new Map();
    const ids = [
      'controlAuth',
      'controlShell',
      'controlLoginForm',
      'controlPasswordForm',
      'controlLoginUser',
      'controlLoginPassword',
      'controlCurrentPassword',
      'controlNewPassword',
      'controlConfirmPassword',
      'controlAuthTitle',
      'controlAuthHint',
      'controlAuthMessage',
      'controlLogout'
    ];
    ids.forEach((id) => this.elements.set(id, new FakeElement(id)));
    if (options.withAuthHosts === false) {
      this.elements.delete('controlAuth');
      this.elements.delete('controlShell');
    }
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }
}

function valueFrom(source, index, ...args) {
  const entry = Array.isArray(source)
    ? source[Math.min(index, source.length - 1)]
    : source;
  if (typeof entry === 'function') return entry(...args);
  return entry;
}

function createHarness(options = {}) {
  const document = options.document || new FakeDocument(options);
  const authCalls = [];
  const loginCalls = [];
  const passwordCalls = [];
  const logoutCalls = [];
  let authenticatedCallbacks = 0;
  let loggedOutCallbacks = 0;
  const authSource = options.fetchPlatformAuthStatus || {
    ok: true,
    enabled: true,
    authenticated: false,
    defaultUser: 'admin'
  };
  const loginSource = options.loginPlatformAuth || {
    ok: true,
    enabled: true,
    authenticated: true,
    mustChangePassword: false
  };
  const passwordSource = options.changePlatformPassword || {
    ok: true,
    enabled: true,
    authenticated: true,
    mustChangePassword: false
  };
  const logoutSource = options.logoutPlatformAuth || { ok: true };

  const controller = authControllerModule.createAuthController({
    document,
    fetchPlatformAuthStatus() {
      const index = authCalls.length;
      authCalls.push(index);
      try {
        return Promise.resolve(valueFrom(authSource, index, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    loginPlatformAuth(username, password) {
      const index = loginCalls.length;
      loginCalls.push({ username, password });
      try {
        return Promise.resolve(valueFrom(loginSource, index, username, password, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    changePlatformPassword(currentPassword, newPassword, confirmPassword) {
      const index = passwordCalls.length;
      passwordCalls.push({ currentPassword, newPassword, confirmPassword });
      try {
        return Promise.resolve(valueFrom(passwordSource, index, currentPassword, newPassword, confirmPassword, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    logoutPlatformAuth() {
      const index = logoutCalls.length;
      logoutCalls.push(index);
      try {
        return Promise.resolve(valueFrom(logoutSource, index, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    onAuthenticated() {
      authenticatedCallbacks += 1;
    },
    onLoggedOut() {
      loggedOutCallbacks += 1;
    }
  });

  return {
    controller,
    document,
    authCalls,
    loginCalls,
    passwordCalls,
    logoutCalls,
    get authenticatedCallbacks() { return authenticatedCallbacks; },
    get loggedOutCallbacks() { return loggedOutCallbacks; }
  };
}

async function main() {
  // Unauthenticated status shows the login form, hides the shell, fills the
  // default user once, and keeps the existing title/hint/message semantics.
  const unauthenticated = createHarness();
  assert.strictEqual(await unauthenticated.controller.ensureAuthenticated(), false);
  assert.strictEqual(unauthenticated.document.getElementById('controlAuth').hidden, false);
  assert.strictEqual(unauthenticated.document.getElementById('controlShell').hidden, true);
  assert.strictEqual(unauthenticated.document.getElementById('controlLoginForm').hidden, false);
  assert.strictEqual(unauthenticated.document.getElementById('controlPasswordForm').hidden, true);
  assert.strictEqual(unauthenticated.document.getElementById('controlLoginUser').value, 'admin');
  assert.strictEqual(unauthenticated.document.getElementById('controlAuthTitle').textContent, '赛事控制台登录');
  assert.strictEqual(unauthenticated.document.getElementById('controlAuthHint').textContent, '输入控制台账号密码后继续。');
  assert.strictEqual(unauthenticated.document.getElementById('controlAuthMessage').textContent, '');

  const existingUser = createHarness();
  existingUser.document.getElementById('controlLoginUser').value = 'operator';
  await existingUser.controller.ensureAuthenticated();
  assert.strictEqual(existingUser.document.getElementById('controlLoginUser').value, 'operator');

  // Authenticated users see the Control shell. First-login password changes
  // retain the distinct form, copy, and password-policy hint.
  const authenticated = createHarness({
    fetchPlatformAuthStatus: { authenticated: true, mustChangePassword: false }
  });
  assert.strictEqual(await authenticated.controller.ensureAuthenticated(), true);
  assert.strictEqual(authenticated.document.getElementById('controlAuth').hidden, true);
  assert.strictEqual(authenticated.document.getElementById('controlShell').hidden, false);

  const mustChange = createHarness({
    fetchPlatformAuthStatus: { authenticated: true, mustChangePassword: true, defaultUser: 'admin' }
  });
  assert.strictEqual(await mustChange.controller.ensureAuthenticated(), false);
  assert.strictEqual(mustChange.document.getElementById('controlShell').hidden, true);
  assert.strictEqual(mustChange.document.getElementById('controlLoginForm').hidden, true);
  assert.strictEqual(mustChange.document.getElementById('controlPasswordForm').hidden, false);
  assert.strictEqual(mustChange.document.getElementById('controlAuthTitle').textContent, '首次登录需要修改密码');
  assert.strictEqual(mustChange.document.getElementById('controlAuthHint').textContent, '默认密码只能用于首次进入，请设置一个新的控制台密码。');
  assert.strictEqual(mustChange.document.getElementById('controlAuthMessage').textContent, '新密码至少 10 位，并包含字母和数字。');

  const failedStatus = createHarness({
    fetchPlatformAuthStatus: { ok: false, authenticated: false, error: '认证服务不可用' }
  });
  assert.strictEqual(await failedStatus.controller.ensureAuthenticated(), false);
  assert.strictEqual(failedStatus.document.getElementById('controlAuthMessage').textContent, '认证服务不可用');
  assert.strictEqual(failedStatus.document.getElementById('controlAuthMessage').className, 'auth-message bad');

  // A transient probe after a reliable authenticated result does not render a
  // false login state or overwrite the cached reliable status.
  const transient = createHarness({
    fetchPlatformAuthStatus: [
      { authenticated: true, mustChangePassword: false },
      { ok: false, authenticated: false, transient: true, error: 'proxy restarting' },
      { authenticated: true, mustChangePassword: false }
    ]
  });
  assert.strictEqual(await transient.controller.ensureAuthenticated(), true);
  transient.document.getElementById('controlAuthMessage').textContent = 'operator result remains';
  assert.strictEqual(await transient.controller.ensureAuthenticated(), true);
  assert.strictEqual(transient.document.getElementById('controlAuth').hidden, true);
  assert.strictEqual(transient.document.getElementById('controlShell').hidden, false);
  assert.strictEqual(transient.document.getElementById('controlAuthMessage').textContent, 'operator result remains');
  assert.strictEqual(await transient.controller.ensureAuthenticated(), true);

  const transientWithoutAuth = createHarness({
    fetchPlatformAuthStatus: { ok: false, authenticated: false, transient: true, error: 'initial probe failed' }
  });
  assert.strictEqual(await transientWithoutAuth.controller.ensureAuthenticated(), false);
  assert.strictEqual(transientWithoutAuth.document.getElementById('controlAuthMessage').textContent, 'initial probe failed');

  // Bind owns exactly the three auth listeners and remains idempotent.
  const bound = createHarness();
  bound.controller.bind();
  bound.controller.bind();
  assert.strictEqual(bound.document.getElementById('controlLoginForm').listenerCount('submit'), 1);
  assert.strictEqual(bound.document.getElementById('controlPasswordForm').listenerCount('submit'), 1);
  assert.strictEqual(bound.document.getElementById('controlLogout').listenerCount('click'), 1);
  assert.strictEqual(bound.document.getElementById('controlLoginForm').dataset.bound, '1');

  // Login trims only the username, preserves the original password, clears it
  // after success, renders auth, and calls the existing Control refresh hook.
  const login = createHarness();
  login.controller.bind();
  login.document.getElementById('controlLoginUser').value = '  operator  ';
  login.document.getElementById('controlLoginPassword').value = '  exact password  ';
  const loginDispatch = login.document.getElementById('controlLoginForm').dispatch('submit');
  assert.strictEqual(loginDispatch.event.defaultPrevented, true);
  assert.strictEqual(login.document.getElementById('controlAuthMessage').textContent, '正在登录...');
  await loginDispatch.promise;
  assert.deepStrictEqual(login.loginCalls, [{ username: 'operator', password: '  exact password  ' }]);
  assert.strictEqual(login.document.getElementById('controlLoginPassword').value, '');
  assert.strictEqual(login.document.getElementById('controlShell').hidden, false);
  assert.strictEqual(login.authenticatedCallbacks, 1);

  const loginFailure = createHarness({
    loginPlatformAuth: () => { throw new Error('密码错误'); }
  });
  loginFailure.controller.bind();
  loginFailure.document.getElementById('controlLoginPassword').value = 'keep-on-failure';
  await loginFailure.document.getElementById('controlLoginForm').dispatch('submit').promise;
  assert.strictEqual(loginFailure.document.getElementById('controlLoginPassword').value, 'keep-on-failure');
  assert.strictEqual(loginFailure.document.getElementById('controlAuthMessage').textContent, '密码错误');
  assert.strictEqual(loginFailure.document.getElementById('controlAuthMessage').className, 'auth-message bad');
  assert.strictEqual(loginFailure.authenticatedCallbacks, 0);

  const mustChangeLogin = createHarness({
    loginPlatformAuth: { authenticated: true, mustChangePassword: true }
  });
  mustChangeLogin.controller.bind();
  mustChangeLogin.document.getElementById('controlLoginPassword').value = 'default-password';
  await mustChangeLogin.document.getElementById('controlLoginForm').dispatch('submit').promise;
  assert.strictEqual(mustChangeLogin.document.getElementById('controlLoginPassword').value, '');
  assert.strictEqual(mustChangeLogin.document.getElementById('controlPasswordForm').hidden, false);
  assert.strictEqual(mustChangeLogin.authenticatedCallbacks, 0);

  // Password mismatch is rejected locally. Success keeps the exact API
  // arguments, clears all fields, renders the returned status, and refreshes.
  const mismatch = createHarness();
  mismatch.controller.bind();
  mismatch.document.getElementById('controlCurrentPassword').value = 'current';
  mismatch.document.getElementById('controlNewPassword').value = 'new-password-1';
  mismatch.document.getElementById('controlConfirmPassword').value = 'new-password-2';
  const mismatchDispatch = mismatch.document.getElementById('controlPasswordForm').dispatch('submit');
  await mismatchDispatch.promise;
  assert.strictEqual(mismatchDispatch.event.defaultPrevented, true);
  assert.strictEqual(mismatch.passwordCalls.length, 0);
  assert.strictEqual(mismatch.document.getElementById('controlAuthMessage').textContent, '两次输入的新密码不一致');

  const password = createHarness();
  password.controller.bind();
  password.document.getElementById('controlCurrentPassword').value = 'current-password';
  password.document.getElementById('controlNewPassword').value = 'new-password-1';
  password.document.getElementById('controlConfirmPassword').value = 'new-password-1';
  await password.document.getElementById('controlPasswordForm').dispatch('submit').promise;
  assert.deepStrictEqual(password.passwordCalls, [{
    currentPassword: 'current-password',
    newPassword: 'new-password-1',
    confirmPassword: 'new-password-1'
  }]);
  assert.strictEqual(password.document.getElementById('controlCurrentPassword').value, '');
  assert.strictEqual(password.document.getElementById('controlNewPassword').value, '');
  assert.strictEqual(password.document.getElementById('controlConfirmPassword').value, '');
  assert.strictEqual(password.document.getElementById('controlShell').hidden, false);
  assert.ok(password.document.getElementById('controlAuthMessage').textHistory.includes('密码已修改'));
  assert.strictEqual(password.authenticatedCallbacks, 1);

  const passwordFailure = createHarness({
    changePlatformPassword: () => { throw new Error('密码策略不满足'); }
  });
  passwordFailure.controller.bind();
  passwordFailure.document.getElementById('controlCurrentPassword').value = 'current';
  passwordFailure.document.getElementById('controlNewPassword').value = 'same-password';
  passwordFailure.document.getElementById('controlConfirmPassword').value = 'same-password';
  await passwordFailure.document.getElementById('controlPasswordForm').dispatch('submit').promise;
  assert.strictEqual(passwordFailure.document.getElementById('controlCurrentPassword').value, 'current');
  assert.strictEqual(passwordFailure.document.getElementById('controlAuthMessage').textContent, '密码策略不满足');
  assert.strictEqual(passwordFailure.authenticatedCallbacks, 0);

  // Logout is best effort: both success and API failure clear app-owned
  // snapshot state through the callback and render the local login state.
  const logout = createHarness({
    fetchPlatformAuthStatus: { authenticated: true, mustChangePassword: false }
  });
  await logout.controller.ensureAuthenticated();
  logout.controller.bind();
  await logout.document.getElementById('controlLogout').dispatch('click').promise;
  assert.strictEqual(logout.logoutCalls.length, 1);
  assert.strictEqual(logout.loggedOutCallbacks, 1);
  assert.strictEqual(logout.document.getElementById('controlAuth').hidden, false);
  assert.strictEqual(logout.document.getElementById('controlShell').hidden, true);
  assert.strictEqual(logout.document.getElementById('controlLoginForm').hidden, false);

  const logoutFailure = createHarness({
    fetchPlatformAuthStatus: { authenticated: true, mustChangePassword: false },
    logoutPlatformAuth: () => { throw new Error('logout endpoint unavailable'); }
  });
  await logoutFailure.controller.ensureAuthenticated();
  logoutFailure.controller.bind();
  await logoutFailure.document.getElementById('controlLogout').dispatch('click').promise;
  assert.strictEqual(logoutFailure.loggedOutCallbacks, 1);
  assert.strictEqual(logoutFailure.document.getElementById('controlAuth').hidden, false);
  assert.strictEqual(logoutFailure.document.getElementById('controlShell').hidden, true);

  // Missing auth hosts preserve the current permissive no-panel behavior.
  const missingHosts = createHarness({
    withAuthHosts: false,
    fetchPlatformAuthStatus: { authenticated: false }
  });
  assert.strictEqual(await missingHosts.controller.ensureAuthenticated(), true);

  console.log('bigscreen auth controller tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
