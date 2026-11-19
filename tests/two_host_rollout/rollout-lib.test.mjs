import assert from "node:assert/strict";
import test from "node:test";
import {
  buildForTransport,
  combineHostResults,
  decideUpdate,
  assertNoSecrets,
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
