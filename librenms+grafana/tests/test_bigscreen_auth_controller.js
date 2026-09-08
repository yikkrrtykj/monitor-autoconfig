const assert = require('assert');
const authControllerModule = require('../bigscreen/control/auth-controller.js');

assert.deepStrictEqual(
  Object.keys(authControllerModule),
  ['createAuthController', 'createControlRefreshLifecycle'],
  'the auth controller exposes its dependency-injected factories'
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
    this._value = '';
    this.valueWrites = 0;
    this.selectionStart = 0;
    this.selectionEnd = 0;
  }

  set textContent(value) {
    this._textContent = String(value);
    this.textHistory.push(this._textContent);
  }

  get textContent() {
    return this._textContent;
  }

  set value(value) {
    this._value = String(value);
    this.valueWrites += 1;
    this.selectionStart = this._value.length;
    this.selectionEnd = this._value.length;
  }

  get value() {
    return this._value;
  }

  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  dispatch(type, init = {}) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      ...init
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
      'controlLoginUser',
      'controlLoginPassword',
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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function createHarness(options = {}) {
  const document = options.document || new FakeDocument(options);
  const authCalls = [];
  const loginCalls = [];
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
    authenticated: true
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
  assert.strictEqual(unauthenticated.document.getElementById('controlPasswordForm'), null);
  assert.strictEqual(unauthenticated.document.getElementById('controlLoginUser').value, 'admin');
  assert.strictEqual(unauthenticated.document.getElementById('controlAuthTitle').textContent, '赛事控制台登录');
  assert.strictEqual(unauthenticated.document.getElementById('controlAuthHint').textContent, '输入控制台账号密码后继续。');
  assert.strictEqual(unauthenticated.document.getElementById('controlAuthMessage').textContent, '');

  const existingUser = createHarness();
  existingUser.document.getElementById('controlLoginUser').value = 'operator';
  await existingUser.controller.ensureAuthenticated();
  assert.strictEqual(existingUser.document.getElementById('controlLoginUser').value, 'operator');

  // Authentication alone opens the Control shell. A legacy server response may
  // still carry mustChangePassword=true, but the retired gate is ignored.
  const authenticated = createHarness({
    fetchPlatformAuthStatus: { authenticated: true, mustChangePassword: false }
  });
  assert.strictEqual(await authenticated.controller.ensureAuthenticated(), true);
  assert.strictEqual(authenticated.document.getElementById('controlAuth').hidden, true);
  assert.strictEqual(authenticated.document.getElementById('controlShell').hidden, false);

  const mustChange = createHarness({
    fetchPlatformAuthStatus: { authenticated: true, mustChangePassword: true, defaultUser: 'admin' }
  });
  assert.strictEqual(await mustChange.controller.ensureAuthenticated(), true);
  assert.strictEqual(mustChange.document.getElementById('controlAuth').hidden, true);
  assert.strictEqual(mustChange.document.getElementById('controlShell').hidden, false);
  assert.strictEqual(mustChange.document.getElementById('controlPasswordForm'), null);

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

  // Bind owns exactly the login and logout listeners and remains idempotent.
  const bound = createHarness();
  bound.controller.bind();
  bound.controller.bind();
  assert.strictEqual(bound.document.getElementById('controlLoginForm').listenerCount('submit'), 1);
  assert.strictEqual(bound.document.getElementById('controlPasswordForm'), null);
  assert.strictEqual(bound.document.getElementById('controlLogout').listenerCount('click'), 1);
  assert.strictEqual(bound.document.getElementById('controlLoginPassword').listenerCount('input'), 1);
  assert.strictEqual(bound.document.getElementById('controlLoginPassword').listenerCount('compositionstart'), 1);
  assert.strictEqual(bound.document.getElementById('controlLoginPassword').listenerCount('compositionend'), 1);
  assert.strictEqual(bound.document.getElementById('controlLoginForm').dataset.bound, '1');

  // Completed input removes non-ASCII without normalizing or rewriting valid
  // ASCII. The cursor follows the retained prefix and the existing message UI
  // reports the cleanup.
  const characterInput = createHarness();
  characterInput.controller.bind();
  const characterPassword = characterInput.document.getElementById('controlLoginPassword');
  characterPassword.value = ' pre-中ａｂｃ１２３，é🙂日本어-post ';
  characterPassword.selectionStart = ' pre-中'.length;
  characterPassword.selectionEnd = characterPassword.selectionStart;
  characterPassword.valueWrites = 0;
  characterPassword.dispatch('input');
  assert.strictEqual(characterPassword.value, ' pre--post ');
  assert.strictEqual(characterPassword.selectionStart, ' pre-'.length);
  assert.strictEqual(characterPassword.selectionEnd, ' pre-'.length);
  assert.strictEqual(characterPassword.valueWrites, 1);
  assert.strictEqual(characterInput.document.getElementById('controlAuthMessage').textContent,
    '密码仅支持英文半角字符，已移除不支持的字符。');
  assert.strictEqual(characterInput.document.getElementById('controlAuthMessage').className, 'auth-message bad');
  characterPassword.value = ' !"#$%&\'()*+,-./09:;<=>?@AZ[\\]^_`az{|}~\u0000\u007f ';
  characterPassword.valueWrites = 0;
  characterPassword.dispatch('input');
  assert.strictEqual(characterPassword.valueWrites, 0);
  assert.strictEqual(characterInput.document.getElementById('controlAuthMessage').textContent,
    '密码仅支持英文半角字符，已移除不支持的字符。');
  assert.deepStrictEqual(characterInput.loginCalls, []);

  // IME candidates remain untouched during composition. Completion applies the
  // same cleanup, while submit during composition is blocked without an API call.
  const composition = createHarness();
  composition.controller.bind();
  const compositionPassword = composition.document.getElementById('controlLoginPassword');
  compositionPassword.dispatch('compositionstart');
  compositionPassword.value = 'abc中文';
  compositionPassword.dispatch('input', { isComposing: true });
  assert.strictEqual(compositionPassword.value, 'abc中文');
  const composingSubmit = composition.document.getElementById('controlLoginForm').dispatch('submit');
  await composingSubmit.promise;
  assert.strictEqual(composingSubmit.event.defaultPrevented, true);
  assert.strictEqual(composition.loginCalls.length, 0);
  assert.strictEqual(compositionPassword.value, 'abc中文');
  compositionPassword.dispatch('compositionend');
  assert.strictEqual(compositionPassword.value, 'abc');
  assert.strictEqual(composition.document.getElementById('controlAuthMessage').textContent,
    '密码仅支持英文半角字符，已移除不支持的字符。');

  // Submit performs an independent guard for autofill or programmatic writes
  // that did not emit input. It cleans and rejects that attempt without using
  // the cleaned value for authentication.
  const guardedSubmit = createHarness();
  guardedSubmit.controller.bind();
  const guardedPassword = guardedSubmit.document.getElementById('controlLoginPassword');
  guardedPassword.value = 'ＡＢＣsecret密码';
  const rejectedSubmit = guardedSubmit.document.getElementById('controlLoginForm').dispatch('submit');
  await rejectedSubmit.promise;
  assert.strictEqual(rejectedSubmit.event.defaultPrevented, true);
  assert.strictEqual(guardedPassword.value, 'secret');
  assert.deepStrictEqual(guardedSubmit.loginCalls, []);

  // A rejected newer submit still starts a new auth lifecycle. Older success
  // and failure results must remain stale and cannot alter the warning or UI.
  for (const outcome of ['success', 'failure']) {
    const pending = deferred();
    const race = createHarness({ loginPlatformAuth: () => pending.promise });
    race.controller.bind();
    const raceForm = race.document.getElementById('controlLoginForm');
    const racePassword = race.document.getElementById('controlLoginPassword');
    race.document.getElementById('controlAuth').hidden = false;
    race.document.getElementById('controlShell').hidden = true;
    racePassword.value = 'first-valid';
    const older = raceForm.dispatch('submit');
    assert.strictEqual(race.loginCalls.length, 1);
    racePassword.value = 'newer非法';
    const rejected = raceForm.dispatch('submit');
    await rejected.promise;
    assert.strictEqual(race.loginCalls.length, 1);
    const warning = race.document.getElementById('controlAuthMessage').textContent;
    if (outcome === 'success') pending.resolve({ authenticated: true });
    else pending.reject(new Error('stale login failure'));
    await older.promise;
    assert.strictEqual(race.authenticatedCallbacks, 0);
    assert.strictEqual(race.document.getElementById('controlAuth').hidden, false);
    assert.strictEqual(race.document.getElementById('controlShell').hidden, true);
    assert.strictEqual(race.document.getElementById('controlAuthMessage').textContent, warning);
  }

  // A submit rejected during composition has the same lifecycle semantics,
  // and a later deliberate ASCII submit can still authenticate normally.
  for (const outcome of ['success', 'failure']) {
    const pending = deferred();
    const compositionRace = createHarness({
      loginPlatformAuth: [() => pending.promise, { authenticated: true }]
    });
    compositionRace.controller.bind();
    const compositionRaceForm = compositionRace.document.getElementById('controlLoginForm');
    const compositionRacePassword = compositionRace.document.getElementById('controlLoginPassword');
    compositionRace.document.getElementById('controlAuth').hidden = false;
    compositionRace.document.getElementById('controlShell').hidden = true;
    compositionRacePassword.value = 'first-valid';
    const older = compositionRaceForm.dispatch('submit');
    compositionRacePassword.dispatch('compositionstart');
    compositionRacePassword.value = '中文候选';
    await compositionRaceForm.dispatch('submit').promise;
    assert.strictEqual(compositionRace.loginCalls.length, 1);
    assert.strictEqual(compositionRace.document.getElementById('controlAuthMessage').textContent,
      '请先完成密码输入后再登录。');
    if (outcome === 'success') pending.resolve({ authenticated: true });
    else pending.reject(new Error('stale composition login failure'));
    await older.promise;
    assert.strictEqual(compositionRace.authenticatedCallbacks, 0);
    assert.strictEqual(compositionRace.document.getElementById('controlAuth').hidden, false);
    assert.strictEqual(compositionRace.document.getElementById('controlShell').hidden, true);
    assert.strictEqual(compositionRace.document.getElementById('controlAuthMessage').textContent,
      '请先完成密码输入后再登录。');
    compositionRacePassword.dispatch('compositionend');
    compositionRacePassword.value = 'later-valid';
    await compositionRaceForm.dispatch('submit').promise;
    assert.strictEqual(compositionRace.loginCalls.length, 2);
    assert.strictEqual(compositionRace.authenticatedCallbacks, 1);
    assert.strictEqual(compositionRace.document.getElementById('controlShell').hidden, false);
  }

  // Login trims only the username, preserves the original password, clears it
  // after success, renders auth, and calls the existing Control refresh hook.
  const login = createHarness();
  login.controller.bind();
  login.document.getElementById('controlLoginUser').value = '  operator  ';
  login.document.getElementById('controlLoginPassword').value = 'global123!@#';
  const loginDispatch = login.document.getElementById('controlLoginForm').dispatch('submit');
  assert.strictEqual(loginDispatch.event.defaultPrevented, true);
  assert.strictEqual(login.document.getElementById('controlAuthMessage').textContent, '正在登录...');
  await loginDispatch.promise;
  assert.deepStrictEqual(login.loginCalls, [{ username: 'operator', password: 'global123!@#' }]);
  assert.strictEqual(login.document.getElementById('controlLoginPassword').value, '');
  assert.strictEqual(login.document.getElementById('controlShell').hidden, false);
  assert.strictEqual(login.authenticatedCallbacks, 1);

  const spacedPassword = createHarness();
  spacedPassword.controller.bind();
  spacedPassword.document.getElementById('controlLoginPassword').value = '  keep spaces !  ';
  await spacedPassword.document.getElementById('controlLoginForm').dispatch('submit').promise;
  assert.deepStrictEqual(spacedPassword.loginCalls, [
    { username: '', password: '  keep spaces !  ' }
  ]);

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
  assert.strictEqual(mustChangeLogin.document.getElementById('controlPasswordForm'), null);
  assert.strictEqual(mustChangeLogin.document.getElementById('controlShell').hidden, false);
  assert.strictEqual(mustChangeLogin.authenticatedCallbacks, 1);

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

  // Refresh results commit in completion order only while their page lifecycle
  // remains active. Starting a later request does not itself cancel a slow one.
  const lifecycle = authControllerModule.createControlRefreshLifecycle();
  lifecycle.start();
  const first = deferred();
  const second = deferred();
  const rendered = [];
  const errors = [];
  const firstRun = lifecycle.execute(() => first.promise, (value) => rendered.push(value), (error) => errors.push(error.message));
  const secondRun = lifecycle.execute(() => second.promise, (value) => rendered.push(value), (error) => errors.push(error.message));
  second.resolve('newer');
  await secondRun;
  first.resolve('older');
  await firstRun;
  assert.deepStrictEqual(rendered, ['newer']);
  assert.deepStrictEqual(errors, []);

  const staleFailure = deferred();
  const currentSuccess = deferred();
  const staleFailureRun = lifecycle.execute(() => staleFailure.promise, (value) => rendered.push(value), (error) => errors.push(error.message));
  const currentSuccessRun = lifecycle.execute(() => currentSuccess.promise, (value) => rendered.push(value), (error) => errors.push(error.message));
  currentSuccess.resolve('current-success');
  await currentSuccessRun;
  staleFailure.reject(new Error('stale failure'));
  await staleFailureRun;
  assert.deepStrictEqual(rendered, ['newer', 'current-success']);
  assert.deepStrictEqual(errors, []);

  const currentFailure = deferred();
  const currentFailureRun = lifecycle.execute(() => currentFailure.promise, (value) => rendered.push(value), (error) => errors.push(error.message));
  currentFailure.reject(new Error('current failure'));
  assert.strictEqual(await currentFailureRun, false);
  assert.deepStrictEqual(errors, ['current failure']);

  const stagedLifecycle = authControllerModule.createControlRefreshLifecycle();
  stagedLifecycle.start();
  const oldAuthStage = deferred();
  const oldDataStage = deferred();
  const stagedRendered = [];
  const oldStagedRun = stagedLifecycle.execute(async () => {
    await oldAuthStage.promise;
    return oldDataStage.promise;
  }, (value) => stagedRendered.push(value), () => {});
  const newStagedRun = stagedLifecycle.execute(async () => {
    await Promise.resolve('authenticated');
    return 'new data';
  }, (value) => stagedRendered.push(value), () => {});
  await newStagedRun;
  oldAuthStage.resolve('old authenticated');
  oldDataStage.resolve('old data');
  await oldStagedRun;
  assert.deepStrictEqual(stagedRendered, ['new data']);

  const slowLifecycle = authControllerModule.createControlRefreshLifecycle();
  slowLifecycle.start();
  const slow = deferred();
  const nextInterval = deferred();
  const slowRendered = [];
  const slowRun = slowLifecycle.execute(() => slow.promise, (value) => slowRendered.push(value), () => {});
  const intervalRun = slowLifecycle.execute(() => nextInterval.promise, (value) => slowRendered.push(value), () => {});
  slow.resolve('slow-valid');
  await slowRun;
  assert.deepStrictEqual(slowRendered, ['slow-valid']);
  nextInterval.resolve('next-valid');
  await intervalRun;
  assert.deepStrictEqual(slowRendered, ['slow-valid', 'next-valid']);

  const pageLifecycle = authControllerModule.createControlRefreshLifecycle();
  pageLifecycle.start();
  const oldPageSuccess = deferred();
  const oldPageFailure = deferred();
  const pageRendered = [];
  const pageErrors = [];
  const oldSuccessRun = pageLifecycle.execute(() => oldPageSuccess.promise, (value) => pageRendered.push(value), (error) => pageErrors.push(error.message));
  const oldFailureRun = pageLifecycle.execute(() => oldPageFailure.promise, (value) => pageRendered.push(value), (error) => pageErrors.push(error.message));
  pageLifecycle.stop();
  oldPageSuccess.resolve('hidden success');
  oldPageFailure.reject(new Error('hidden failure'));
  await Promise.all([oldSuccessRun, oldFailureRun]);
  assert.deepStrictEqual(pageRendered, []);
  assert.deepStrictEqual(pageErrors, []);

  pageLifecycle.start();
  const priorPage = deferred();
  const priorRun = pageLifecycle.execute(() => priorPage.promise, (value) => pageRendered.push(value), () => {});
  pageLifecycle.stop();
  pageLifecycle.start();
  const currentPage = deferred();
  const currentRun = pageLifecycle.execute(() => currentPage.promise, (value) => pageRendered.push(value), () => {});
  currentPage.resolve('current page');
  await currentRun;
  priorPage.resolve('prior page');
  await priorRun;
  assert.deepStrictEqual(pageRendered, ['current page']);

  const applyLifecycle = authControllerModule.createControlRefreshLifecycle();
  applyLifecycle.start();
  const duringApply = deferred();
  const afterApply = deferred();
  const applyRendered = [];
  const duringApplyRun = applyLifecycle.execute(() => duringApply.promise, (value) => applyRendered.push(value), () => {});
  const afterApplyRun = applyLifecycle.execute(() => afterApply.promise, (value) => applyRendered.push(value), () => {});
  applyLifecycle.invalidate();
  duringApply.resolve('returned during apply');
  await duringApplyRun;
  const postApplyRun = applyLifecycle.execute(async () => 'post-apply result', (value) => applyRendered.push(value), () => {});
  await postApplyRun;
  afterApply.resolve('returned after apply');
  await afterApplyRun;
  assert.deepStrictEqual(applyRendered, ['post-apply result']);

  // Logout invalidates probes and renders the logged-out state before the
  // best-effort network request resolves.
  const logoutProbe = deferred();
  const logoutRequest = deferred();
  const logoutRace = createHarness({
    fetchPlatformAuthStatus: () => logoutProbe.promise,
    logoutPlatformAuth: () => logoutRequest.promise
  });
  logoutRace.controller.bind();
  const oldAuthenticatedProbe = logoutRace.controller.ensureAuthenticated();
  const logoutDispatch = logoutRace.document.getElementById('controlLogout').dispatch('click');
  assert.strictEqual(logoutRace.document.getElementById('controlAuth').hidden, false);
  assert.strictEqual(logoutRace.document.getElementById('controlShell').hidden, true);
  logoutProbe.resolve({ authenticated: true });
  assert.strictEqual(await oldAuthenticatedProbe, null);
  assert.strictEqual(logoutRace.document.getElementById('controlShell').hidden, true);
  logoutRequest.resolve({ ok: true });
  await logoutDispatch.promise;

  const oldTransient = deferred();
  const transientLogout = createHarness({
    fetchPlatformAuthStatus: [
      { authenticated: true },
      () => oldTransient.promise
    ]
  });
  assert.strictEqual(await transientLogout.controller.ensureAuthenticated(), true);
  const transientProbe = transientLogout.controller.ensureAuthenticated();
  transientLogout.controller.bind();
  await transientLogout.document.getElementById('controlLogout').dispatch('click').promise;
  oldTransient.resolve({ ok: false, authenticated: false, transient: true, error: 'old transient' });
  assert.strictEqual(await transientProbe, null);
  assert.strictEqual(transientLogout.document.getElementById('controlAuth').hidden, false);
  assert.strictEqual(transientLogout.document.getElementById('controlShell').hidden, true);

  // A new login lifecycle wins over an older unauthenticated probe.
  const oldUnauthenticated = deferred();
  const relogin = createHarness({
    fetchPlatformAuthStatus: () => oldUnauthenticated.promise,
    loginPlatformAuth: { authenticated: true }
  });
  relogin.controller.bind();
  const oldUnauthenticatedProbe = relogin.controller.ensureAuthenticated();
  relogin.document.getElementById('controlLoginUser').value = 'operator';
  relogin.document.getElementById('controlLoginPassword').value = 'secret';
  await relogin.document.getElementById('controlLoginForm').dispatch('submit').promise;
  oldUnauthenticated.resolve({ authenticated: false, error: 'old session' });
  assert.strictEqual(await oldUnauthenticatedProbe, null);
  assert.strictEqual(relogin.document.getElementById('controlAuth').hidden, true);
  assert.strictEqual(relogin.document.getElementById('controlShell').hidden, false);

  console.log('bigscreen auth controller tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
