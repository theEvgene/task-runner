import assert from "node:assert/strict";
import test from "node:test";
import {
  DUAL_HOST_SCHEMA,
  PAT_SCHEMA,
  combineDualHostResults,
  verifyDualLenovoPayload,
  verifyDualReceipt,
  verifyPatReceipt,
} from "../../scripts/two_host_rollout/dual-lib.mjs";
import { HOST_SCHEMA } from "../../scripts/two_host_rollout/lib.mjs";

const TRT = "a".repeat(40);
const PAT = "b".repeat(40);

function trReceipt(over = {}) {
  return {
    schema: HOST_SCHEMA,
    verdict: "updated",
    reason: "updated",
    targetSha: TRT,
    isLatest: true,
    branch: "main",
    head: TRT,
    originMain: TRT,
    clean: true,
    skills: { state: "current" },
    ...over,
  };
}

function patReceipt(over = {}) {
  return {
    schema: PAT_SCHEMA,
    ok: true,
    requestedRevision: PAT,
    pinnedRevision: PAT,
    installedRevision: PAT,
    installedCheckout: "C:\\pat",
    verification: {
      installedCheckoutHead: PAT,
      headMatchesPinnedRevision: true,
      checkoutClean: true,
      verificationPasses: 1,
      junctions: { destination: "s", expected: 1, matched: 1, mismatched: [] },
      verificationErrors: [],
      installProblems: [],
      verified: true,
    },
    mutation: "installed-checkout-advanced",
    ...over,
  };
}

function dual(over = {}) {
  return {
    schema: DUAL_HOST_SCHEMA,
    verdict: "updated",
    reason: "updated",
    taskRunnerTarget: TRT,
    patTarget: PAT,
    isLatest: true,
    checkout: "C:\\tr",
    patCheckout: "C:\\pat",
    taskRunner: trReceipt(),
    pat: patReceipt(),
    ...over,
  };
}

const targets = { taskRunnerTarget: TRT, patTarget: PAT, isLatest: true };

test("four combos verify: both hosts x both repos", () => {
  const r = combineDualHostResults([dual(), dual()], targets);
  assert.equal(r.verdict, "updated");
  assert.equal(verifyDualReceipt(dual(), targets).ok, true);
});

test("one refused host is partial, never updated", () => {
  const refused = dual({
    verdict: "refused",
    reason: "dirty-checkout",
    detail: "commit or stash",
  });
  const r = combineDualHostResults([refused, dual()], targets);
  assert.equal(r.verdict, "partial");
  assert.equal(r.reason, "dirty-checkout");
  // Order-independent: lenovo-first also partial.
  const r2 = combineDualHostResults([dual(), refused], targets);
  assert.equal(r2.verdict, "partial");
});

test("one stale repo refuses the whole run", () => {
  const stalePat = dual({
    pat: patReceipt({ pinnedRevision: "c".repeat(40) }),
  });
  const checked = verifyDualReceipt(stalePat, targets);
  assert.equal(checked.ok, false);
  assert.equal(checked.reason, "revision-mismatch");
  const combined = combineDualHostResults([stalePat, dual()], targets);
  assert.notEqual(combined.verdict, "updated");
});

test("PAT receipt enforces the native evidence scheme", () => {
  assert.equal(verifyPatReceipt(patReceipt(), { target: PAT }).ok, true);
  // ok false is a terminal installer refusal, not success.
  assert.equal(
    verifyPatReceipt(patReceipt({ ok: false, failure: { reason: "x", message: "y" } }), { target: PAT }).ok,
    false
  );
  // Exactly one verification pass; zero or two is a mismatch.
  assert.equal(verifyPatReceipt(patReceipt({ verification: { ...patReceipt().verification, verificationPasses: 0 } }), { target: PAT }).reason, "pat-readback-mismatch");
  // Dirty PAT checkout refuses.
  assert.equal(
    verifyPatReceipt(patReceipt({ verification: { ...patReceipt().verification, checkoutClean: false } }), { target: PAT }).reason,
    "pat-dirty-checkout"
  );
  // Non-empty mismatched/errors/problems refuse.
  const v = patReceipt().verification;
  assert.equal(
    verifyPatReceipt(patReceipt({ verification: { ...v, junctions: { mismatched: ["x"] } } }), { target: PAT }).ok,
    false
  );
  // Receipt bound to another checkout cannot be reused.
  assert.equal(
    verifyPatReceipt(patReceipt(), { target: PAT, checkout: "C:\\other" }).reason,
    "pat-checkout-mismatch"
  );
  // Bad schema and bad target fail closed.
  assert.equal(verifyPatReceipt({ schema: "x" }, { target: PAT }).ok, false);
  assert.equal(verifyPatReceipt(patReceipt(), { target: "zzz" }).ok, false);
});

test("transport states: unknown stays unknown, terminal needs a new id", () => {
  // Empty slot: unknown, re-fetch the SAME id (never a new submit).
  const unknown = verifyDualLenovoPayload({ status: "completed", stdout: "" }, targets);
  assert.equal(unknown.ok, false);
  assert.equal(unknown.reason, "receipt-missing");
  // Worker timeout / truncation are terminal: NEW id only after fixing.
  assert.equal(verifyDualLenovoPayload({ timed_out: true }, targets).reason, "worker-timeout");
  assert.equal(verifyDualLenovoPayload({ output_truncated: true }, targets).reason, "output-truncated");
  // Verified stdout carrying a refused dual receipt is terminal partial input.
  const refusedStdout = JSON.stringify(dual({ verdict: "refused", reason: "dirty-checkout" }));
  const refused = verifyDualLenovoPayload({ status: "error", stdout: refusedStdout }, targets);
  assert.equal(refused.ok, true);
  assert.equal(refused.refused, true);
  // Unparsable stdout never verifies.
  assert.equal(
    verifyDualLenovoPayload({ status: "completed", stdout: "not json" }, targets).reason,
    "receipt-unparsable"
  );
});

test("secrets and cross-pin reuse are refused", () => {
  const leaked = dual({
    taskRunner: trReceipt({ detail: "token ghp_abc123XYZ" }),
  });
  assert.equal(verifyDualReceipt(leaked, targets).ok, false);
  const crossPat = dual({ patTarget: "c".repeat(40) });
  assert.equal(verifyDualReceipt(crossPat, targets).reason, "revision-mismatch");
});
