#!/usr/bin/env node
// Pure dual-repo policy for issue #162 (includes #159). Two pinned SHAs,
// four combos (Dell/ Lenovo x task-runner / PAT). No git, no fs, no network:
// executables supply facts, tests supply fixtures.
//
// Native evidence schemes (observed, never invented):
// - task-runner per-host receipt: scripts/two_host_rollout/apply-host.mjs
//   writes schema two-host-rollout-host/1 (see docs/two-host-rollout.md).
// - PAT per-host receipt: <pat-checkout>/bootstrap/rollout.ps1 writes schema
//   personal-agent-toolkit/skill-rollout@1 (see that repo's
//   docs/agents/skill-rollout.md: ok, pinnedRevision, installedRevision,
//   verification.verified, exactly one verification pass, clean checkout,
//   empty mismatched/errors/problems lists).
// A dual-host receipt bundles one of each plus the two checkout paths, so
// every host/repository combo shows checkout, revision, install and readback.
import {
  HOST_SCHEMA,
  SHA_RE,
  assertNoSecrets,
} from "./lib.mjs";

export const DUAL_HOST_SCHEMA = "two-host-dual-host/1";
export const DUAL_REQUEST_SCHEMA = "two-host-dual-rollout-request/1";
export const PAT_SCHEMA = "personal-agent-toolkit/skill-rollout@1";

function fail(reason, detail = "") {
  return { ok: false, reason, detail: String(detail).slice(0, 400) };
}

// PAT receipt check against the pinned PAT SHA. Terminal refusal (fix then
// rerun the same revision); unknown only ever comes from the transport wait,
// never from a parsed receipt.
export function verifyPatReceipt(receipt, { target, checkout } = {}) {
  const t = String(target ?? "").toLowerCase();
  if (!SHA_RE.test(t)) return fail("bad-target", "PAT target is not a SHA");
  if (!receipt || typeof receipt !== "object")
    return fail("pat-receipt-missing", "no PAT receipt object");
  if (receipt.schema !== PAT_SCHEMA)
    return fail("pat-receipt-malformed", "bad PAT schema");
  try {
    assertNoSecrets("pat receipt", JSON.stringify(receipt));
  } catch (error) {
    return fail("pat-receipt-secret", error.message);
  }
  if (receipt.ok !== true)
    return fail(
      "pat-install-failed",
      receipt?.failure
        ? `${receipt.failure.reason ?? "failed"}: ${receipt.failure.message ?? ""}`.slice(0, 300)
        : "PAT installer ok != true (terminal refusal, fix then rerun same revision)"
    );
  if (String(receipt.pinnedRevision ?? "").toLowerCase() !== t)
    return fail("revision-mismatch", "PAT pinnedRevision != target");
  if (String(receipt.installedRevision ?? "").toLowerCase() !== t)
    return fail("revision-mismatch", "PAT installedRevision != target");
  const v = receipt.verification ?? {};
  if (v.verified !== true)
    return fail("pat-readback-mismatch", "PAT verification.verified != true");
  if (v.verificationPasses !== 1)
    return fail("pat-readback-mismatch", "PAT verificationPasses != 1");
  if (v.checkoutClean !== true)
    return fail("pat-dirty-checkout", "PAT checkout is not clean");
  if (!String(v.installedCheckoutHead ?? "").toLowerCase().startsWith(t.slice(0, 7)) || String(v.installedCheckoutHead ?? "").toLowerCase() !== t)
    return fail("revision-mismatch", "PAT installedCheckoutHead != target");
  if (Array.isArray(v.junctions?.mismatched) && v.junctions.mismatched.length > 0)
    return fail("pat-readback-mismatch", `PAT junctions mismatched: ${v.junctions.mismatched.join(",")}`);
  if (Array.isArray(v.verificationErrors) && v.verificationErrors.length > 0)
    return fail("pat-readback-mismatch", `PAT verificationErrors: ${v.verificationErrors.join(";").slice(0, 200)}`);
  if (Array.isArray(v.installProblems) && v.installProblems.length > 0)
    return fail("pat-install-failed", `PAT installProblems: ${v.installProblems.join(";").slice(0, 200)}`);
  if (checkout) {
    const got = String(receipt.installedCheckout ?? "").toLowerCase();
    if (got !== String(checkout).toLowerCase())
      return fail("pat-checkout-mismatch", "PAT receipt checkout != this host checkout");
  }
  return { ok: true };
}

// Task-runner sub-receipt check: same rules as verify-readback.mjs single
// host (branch/main, head/clean, originMain for latest, skills current).
export function verifyTaskRunnerSubReceipt(receipt, { target, isLatest }) {
  const t = String(target ?? "").toLowerCase();
  if (!SHA_RE.test(t)) return fail("bad-target", "task-runner target is not a SHA");
  if (!receipt || typeof receipt !== "object")
    return fail("receipt-missing", "no task-runner receipt object");
  if (receipt.schema !== HOST_SCHEMA)
    return fail("receipt-malformed", "bad task-runner schema");
  try {
    assertNoSecrets("task-runner receipt", JSON.stringify(receipt));
  } catch (error) {
    return fail("receipt-secret", error.message);
  }
  if (!["updated", "already-current"].includes(receipt.verdict))
    return {
      ok: true,
      refused: true,
      reason: receipt.reason ?? "host-failed",
      detail: String(receipt.detail ?? "").slice(0, 300),
    };
  if (String(receipt.targetSha ?? "").toLowerCase() !== t)
    return fail("revision-mismatch", "task-runner target mismatch");
  if (Boolean(receipt.isLatest) !== Boolean(isLatest))
    return fail("revision-mismatch", "task-runner isLatest flag mismatch");
  if (String(receipt.head ?? "").toLowerCase() !== t || receipt.clean !== true)
    return fail("revision-mismatch", "task-runner head/clean mismatch");
  if (Boolean(isLatest)) {
    if (receipt.branch !== "main")
      return fail("wrong-branch", "task-runner latest receipt is not on main");
    if (String(receipt.originMain ?? "").toLowerCase() !== t)
      return fail("revision-mismatch", "task-runner originMain != target");
  }
  if (receipt.skills?.state !== "current")
    return fail("skills-not-current", `task-runner skills ${receipt.skills?.state ?? "unknown"}`);
  return { ok: true, refused: false };
}

function nextActionFor(reason) {
  return (
    {
      updated: "both dual-host receipts verify: all four combos current",
      "already-current": "no change on either repo; rerun is safe",
      "dirty-checkout": "commit or stash local changes, then rerun the same two revisions",
      diverged: "rebase local commits onto the published revision; never force",
      "wrong-branch": "move task-runner checkout to main (latest) or rerun pinned explicitly",
      "revision-mismatch": "rerun with the two verified SHAs, not guessed ones",
      "ownership-conflict": "remove only foreign or stale skill dirs manually, then rerun; nothing was overwritten",
      "partial-install": "complete the interrupted install manually or clear the skills dir, then rerun",
      "install-failed": "read installer output, fix the runtime checkout, then rerun the same revisions",
      "pat-install-failed": "read the PAT receipt failure, fix that checkout, then rerun the same PAT SHA",
      "pat-dirty-checkout": "PAT rollout stashes tracked changes itself only when clean-stashable; resolve the PAT checkout, then rerun the same PAT SHA",
      "pat-readback-mismatch": "rerun the PAT rollout readback; a changed input invalidates the PAT receipt",
      "pat-checkout-mismatch": "fetch the receipt bound to this host checkout, never another checkout",
      "pat-receipt-missing": "stop and reconcile; a missing PAT receipt is never success",
      unknown: "re-fetch the SAME job id; never submit a new id while unknown",
      "outcome-unknown": "re-fetch the SAME job id; never submit a new id while unknown",
      "worker-timeout": "terminal worker timeout: fix limits, then a NEW job id",
      "output-truncated": "terminal incomplete stdout: rerun as a NEW id with larger --max-output",
    }[reason] ?? "stop and reconcile before any retry"
  );
}

// One dual-host receipt: both repos verified against their own pin on one
// host. checkout/patCheckout bind each sub-receipt to this host's paths so a
// receipt from another checkout cannot be reused silently.
export function verifyDualReceipt(dual, { taskRunnerTarget, patTarget, isLatest }) {
  const trt = String(taskRunnerTarget ?? "").toLowerCase();
  const pt = String(patTarget ?? "").toLowerCase();
  if (!SHA_RE.test(trt) || !SHA_RE.test(pt))
    return fail("bad-target", "dual targets must be full SHAs");
  if (!dual || typeof dual !== "object")
    return fail("receipt-missing", "no dual-host receipt");
  if (dual.schema !== DUAL_HOST_SCHEMA)
    return fail("receipt-malformed", "bad dual-host schema");
  try {
    assertNoSecrets("dual receipt", JSON.stringify(dual));
  } catch (error) {
    return fail("receipt-secret", error.message);
  }
  if (dual.verdict === "refused")
    return {
      ok: true,
      refused: true,
      reason: dual.reason ?? "host-failed",
      detail: String(dual.detail ?? "").slice(0, 300),
    };
  if (!["updated", "already-current"].includes(dual.verdict))
    return fail("receipt-malformed", `bad dual verdict ${dual.verdict}`);
  if (
    String(dual.taskRunnerTarget ?? "").toLowerCase() !== trt ||
    String(dual.patTarget ?? "").toLowerCase() !== pt ||
    Boolean(dual.isLatest) !== Boolean(isLatest)
  )
    return fail("revision-mismatch", "dual receipt targets != requested pins");
  const tr = verifyTaskRunnerSubReceipt(dual.taskRunner, {
    target: trt,
    isLatest,
  });
  if (!tr.ok) return tr;
  if (tr.refused)
    return { ok: true, refused: true, reason: tr.reason, detail: tr.detail };
  const pat = verifyPatReceipt(dual.pat, { target: pt, checkout: dual.patCheckout });
  if (!pat.ok) return pat;
  return { ok: true, refused: false };
}

// Success only when all four combos verify (2 hosts x 2 repos). Any refused
// host is partial, never updated; unreadable input is mismatch.
export function combineDualHostResults(hosts, { taskRunnerTarget, patTarget, isLatest }) {
  if (!Array.isArray(hosts) || hosts.length === 0)
    return {
      verdict: "partial",
      reason: "host-failed",
      detail: "no host receipts",
      nextAction: nextActionFor("host-failed"),
    };
  for (const h of hosts) {
    const checked = verifyDualReceipt(h, { taskRunnerTarget, patTarget, isLatest });
    if (!checked.ok) {
      const terminal = ["worker-timeout", "output-truncated"].includes(checked.reason);
      return {
        verdict: terminal ? "mismatch" : checked.reason === "revision-mismatch" ? "mismatch" : "partial",
        reason: checked.reason,
        detail: checked.detail,
        nextAction: nextActionFor(checked.reason),
      };
    }
    if (checked.refused) {
      return {
        verdict: "partial",
        reason: checked.reason ?? "host-failed",
        detail: checked.detail ?? "",
        nextAction: nextActionFor(checked.reason ?? "host-failed"),
      };
    }
  }
  return {
    verdict: "updated",
    reason: "updated",
    detail: `${hosts.length} hosts x 2 repos verified`,
    nextAction: nextActionFor("updated"),
  };
}

// Authenticated Lenovo stdout check for the dual run. Transport first
// (same sealed-envelope rules as lib.verifyLenovoPayload), receipt second.
export function verifyDualLenovoPayload(payload, { taskRunnerTarget, patTarget, isLatest }) {
  const failDual = (reason, detail = "") => ({
    ok: false,
    reason,
    detail: String(detail).slice(0, 400),
  });
  const trt = String(taskRunnerTarget ?? "").toLowerCase();
  const pt = String(patTarget ?? "").toLowerCase();
  if (!SHA_RE.test(trt) || !SHA_RE.test(pt))
    return failDual("bad-target", "dual targets must be full SHAs");
  if (!payload || typeof payload !== "object")
    return failDual("receipt-missing", "no verified payload");
  if (payload.timed_out)
    return failDual("worker-timeout", "terminal worker timeout: fix limits, then a NEW job id");
  if (payload.output_truncated)
    return failDual("output-truncated", "terminal incomplete stdout: rerun as a NEW id with larger --max-output");
  if (!["completed", "ok", "error"].includes(payload.status))
    return failDual("unknown-status", `verified envelope, unrecognized status=${String(payload.status)}`);
  const stdout = payload.stdout;
  if (typeof stdout !== "string" || stdout.trim() === "")
    return failDual("receipt-missing", "verified envelope carries no stdout");
  let dual;
  try {
    dual = JSON.parse(stdout);
  } catch (error) {
    return failDual("receipt-unparsable", error.message);
  }
  try {
    assertNoSecrets("lenovo dual receipt", stdout);
  } catch (error) {
    return failDual("receipt-secret", error.message);
  }
  const checked = verifyDualReceipt(dual, { taskRunnerTarget: trt, patTarget: pt, isLatest });
  if (!checked.ok) return failDual(checked.reason, checked.detail);
  return { ok: true, refused: Boolean(checked.refused), receipt: dual };
}
