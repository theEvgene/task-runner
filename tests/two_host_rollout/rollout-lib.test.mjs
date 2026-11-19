import assert from "node:assert/strict";
import test from "node:test";
import {
  buildForTransport,
  combineHostResults,
  decideUpdate,
  assertNoSecrets,
  verifyLenovoPayload,
} from "../../scripts/two_host_rollout/lib.mjs";

test("latest fast-forward matrix", () => {
  const base = {
    originMain: "a".repeat(40),
    target: "a".repeat(40),
    dirty: false,
    isLatest: true,
  };
  assert.equal(
    decideUpdate({
      ...base,
      branch: "main",
      head: "a".repeat(40),
      ancestorOk: true,
    }).action,
    "already-current"
  );
  assert.equal(
    decideUpdate({
      ...base,
      branch: null,
      head: "a".repeat(40),
      ancestorOk: true,
    }).action,
    "checkout-main-ff"
  );
  assert.equal(
    decideUpdate({
      ...base,
      branch: "main",
      head: "b".repeat(40),
      ancestorOk: true,
    }).action,
    "fast-forward"
  );
  assert.equal(
    decideUpdate({
      ...base,
      branch: "main",
      head: "b".repeat(40),
      ancestorOk: false,
    }).reason,
    "diverged"
  );
  assert.equal(
    decideUpdate({
      ...base,
      branch: "feature",
      head: "b".repeat(40),
      ancestorOk: true,
    }).reason,
    "wrong-branch"
  );
  assert.equal(
    decideUpdate({
      ...base,
      branch: "main",
      head: "b".repeat(40),
      dirty: true,
      ancestorOk: true,
    }).reason,
    "dirty-checkout"
  );
  assert.equal(
    decideUpdate({
      ...base,
      target: "c".repeat(40),
      branch: "main",
      head: "b".repeat(40),
      ancestorOk: true,
    }).reason,
    "revision-mismatch"
  );
});

test("pinned older revision never moves main back", () => {
  const old = "b".repeat(40);
  const now = "a".repeat(40);
  const r = decideUpdate({
    branch: "main",
    head: now,
    originMain: now,
    target: old,
    dirty: false,
    isLatest: false,
    ancestorOk: false,
  });
  assert.equal(r.ok, true);
  assert.equal(r.action, "checkout-detached");
  assert.equal(r.finalBranch, "detached");
  const same = decideUpdate({
    branch: null,
    head: old,
    originMain: now,
    target: old,
    dirty: false,
    isLatest: false,
    ancestorOk: false,
  });
  assert.equal(same.action, "already-current");
});

test("secrets and unsafe transport are refused", () => {
  assert.throws(
    () => assertNoSecrets("argv", "x JIRA_SESSION_COOKIE=y"),
    /secret/
  );
  assert.throws(() => assertNoSecrets("cmd", "echo ghp_abc123XYZ"), /secret/);
  assert.throws(() => buildForTransport(["node", 'a"b']), /double quote/);
  const cmd = buildForTransport([
    "node",
    "C:\\r\\apply-host.mjs",
    "--target-sha",
    "a".repeat(40),
  ]);
  assert.match(
    cmd,
    /^powershell\.exe -NoProfile -NonInteractive -Command "& '/
  );
});

test("partial success is never reported as full", () => {
  const t = "a".repeat(40);
  const both = combineHostResults(
    [
      { verdict: "updated", head: t },
      { verdict: "updated", head: t },
    ],
    t
  );
  assert.equal(both.verdict, "updated");
  const partial = combineHostResults(
    [
      { verdict: "updated", head: t },
      { verdict: "refused", reason: "dirty-checkout", head: "b".repeat(40) },
    ],
    t
  );
  assert.equal(partial.verdict, "partial");
  assert.equal(partial.reason, "dirty-checkout");
});

test("verified lenovo payload: transport first, receipt authoritative", () => {
  const t = "a".repeat(40);
  const host = (over = {}) => ({
    schema: "two-host-rollout-host/1",
    verdict: "updated",
    targetSha: t,
    isLatest: true,
    branch: "main",
    head: t,
    originMain: t,
    clean: true,
    skills: { state: "current" },
    ...over,
  });
  const ok = verifyLenovoPayload(
    { status: "completed", exit_code: 0, timed_out: false,
      output_truncated: false, stdout: JSON.stringify(host()) },
    { target: t, isLatest: true }
  );
  assert.equal(ok.ok, true);
  // A refused host receipt arrives as an error envelope yet stays a
  // verified terminal outcome: accepted here, partial at combine time.
  const refused = verifyLenovoPayload(
    { status: "error", exit_code: 1, timed_out: false,
      output_truncated: false,
      stdout: JSON.stringify(host({ verdict: "refused", reason: "dirty-checkout" })) },
    { target: t, isLatest: true }
  );
  assert.equal(refused.ok, true);
  assert.equal(refused.refused, true);
  // Terminal transport states never verify: NEW id only, never a retry here.
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: true,
        output_truncated: false, stdout: JSON.stringify(host()) },
      { target: t, isLatest: true }
    ).reason,
    "worker-timeout"
  );
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: false,
        output_truncated: true, stdout: JSON.stringify(host()) },
      { target: t, isLatest: true }
    ).reason,
    "output-truncated"
  );
  assert.equal(
    verifyLenovoPayload(
      { status: "weird", exit_code: 0, timed_out: false,
        output_truncated: false, stdout: JSON.stringify(host()) },
      { target: t, isLatest: true }
    ).reason,
    "unknown-status"
  );
  // Receipt binding: wrong target, wrong flag, wrong branch, stale skills.
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: false,
        output_truncated: false,
        stdout: JSON.stringify(host({ targetSha: "b".repeat(40) })) },
      { target: t, isLatest: true }
    ).reason,
    "revision-mismatch"
  );
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: false,
        output_truncated: false,
        stdout: JSON.stringify(host({ branch: "detached" })) },
      { target: t, isLatest: true }
    ).reason,
    "wrong-branch"
  );
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: false,
        output_truncated: false,
        stdout: JSON.stringify(host({ originMain: "b".repeat(40) })) },
      { target: t, isLatest: true }
    ).reason,
    "revision-mismatch"
  );
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: false,
        output_truncated: false,
        stdout: JSON.stringify(host({ skills: { state: "partial-install" } })) },
      { target: t, isLatest: true }
    ).reason,
    "skills-not-current"
  );
  assert.equal(
    verifyLenovoPayload(
      { status: "completed", exit_code: 0, timed_out: false,
        output_truncated: false, stdout: "not-json" },
      { target: t, isLatest: true }
    ).reason,
    "receipt-unparsable"
  );
});
