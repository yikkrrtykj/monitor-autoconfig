const assert = require("assert");
const path = require("path");

const {
  APPLY_REQUEST_TIMEOUT_MS,
  APPLY_RECOVERY_FALLBACK_MS,
  classifyApplyStatus,
  waitForApplyRecovery,
  applyRecoveryRenderPayload
} = require(path.resolve(__dirname, "../bigscreen/platform.js"));

function fakeRecoveryOptions(statuses, maxMs = 25) {
  let clock = 0;
  let index = 0;
  return {
    maxMs,
    initialDelayMs: 0,
    pollIntervalMs: 10,
    deadlineGraceMs: 0,
    now: () => clock,
    sleep: async (milliseconds) => { clock += milliseconds; },
    fetchConfig: async () => ({ ok: true, issues: [] }),
    fetchStatus: async () => statuses[Math.min(index++, statuses.length - 1)]
  };
}

async function main() {
  assert.ok(APPLY_RECOVERY_FALLBACK_MS > APPLY_REQUEST_TIMEOUT_MS);
  assert.strictEqual(classifyApplyStatus({ state: "running" }), "running");
  assert.strictEqual(classifyApplyStatus({ state: "succeeded" }), "succeeded");
  assert.strictEqual(classifyApplyStatus({ state: "pending" }), "pending");
  assert.strictEqual(classifyApplyStatus({ state: "failed" }), "failed");
  assert.strictEqual(classifyApplyStatus({ state: "unavailable" }), "unknown");

  const stillRunning = await waitForApplyRecovery(
    "web-apply-running",
    fakeRecoveryOptions([{ state: "running", operationId: "web-apply-running" }])
  );
  assert.strictEqual(stillRunning.outcome, "running");
  const runningView = applyRecoveryRenderPayload(stillRunning, "apply");
  assert.strictEqual(runningView.pending, true);
  assert.notStrictEqual(runningView.ok, false, "running task must not render as a failure");

  // A backend deadline extends a short local fallback, so a terminal status
  // arriving after the original browser window is still recovered.
  const extended = await waitForApplyRecovery(
    "web-apply-extended",
    fakeRecoveryOptions([
      { state: "running", deadlineAt: 0.05 },
      { ok: true, state: "succeeded", applied: true, operationId: "web-apply-extended" }
    ], 10)
  );
  assert.strictEqual(extended.outcome, "succeeded");
  const successView = applyRecoveryRenderPayload(extended, "apply");
  assert.strictEqual(successView.ok, true);
  assert.strictEqual(successView.applied, true);

  const failed = await waitForApplyRecovery(
    "web-apply-failed",
    fakeRecoveryOptions([{
      ok: false,
      state: "failed",
      operationId: "web-apply-failed",
      error: "automatic apply timed out"
    }])
  );
  assert.strictEqual(failed.outcome, "failed");
  const failedView = applyRecoveryRenderPayload(failed, "apply");
  assert.strictEqual(failedView.ok, false);
  assert.strictEqual(failedView.error, "automatic apply timed out");

  const unknown = await waitForApplyRecovery(
    "web-apply-unknown",
    fakeRecoveryOptions([{ ok: false, state: "unavailable" }])
  );
  assert.strictEqual(unknown.outcome, "unknown");
  assert.strictEqual(
    applyRecoveryRenderPayload(unknown, "apply").errorTitle,
    "无法确认应用结果"
  );

  console.log("bigscreen apply recovery tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
