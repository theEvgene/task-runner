#!/usr/bin/env node
// Pure rollout policy for issue #133. No git, no fs, no network here:
// apply-host.mjs supplies facts, tests supply fixtures.
export const SHA_RE = /^[0-9a-f]{40}$|^[0-9a-f]{64}$/i;
export const JOB_RE = /^[0-9a-f]{32}$/;
export const HOST_SCHEMA = "two-host-rollout-host/1";
export const REQUEST_SCHEMA = "two-host-rollout-request/1";

const SECRET_RE =
  /JIRA_SESSION_COOKIE|JIRA_ATL_TOKEN|DCO_(TEST|PRODUCTION|ACCESS)_TOKEN|BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+/;

export function assertNoSecrets(label, text) {
  if (SECRET_RE.test(String(text ?? ""))) {
    throw new Error(`${label} carries secret material: refused`);
  }
}

export function assertSafeCommandPart(label, value) {
  if (/["$`%\x00-\x1f\x7f]/.test(String(value ?? ""))) {
    throw new Error(
      `${label} contains characters unsafe for the signed command`
    );
  }
}

export function psQuote(part) {
  return `'${String(part).replace(/'/g, "''")}'`;
}

// Port of scripts/dell_lan/shell_command.py build_for_transport for the
// powershell dialect only: literal program, one double-quoted -Command.
export function buildForTransport(command) {
  if (!Array.isArray(command) || command.length === 0) {
    throw new Error("command must contain at least one part");
  }
  for (const part of command) {
    if (typeof part !== "string")
      throw new Error("command parts must be strings");
    if (/[\x00\r\n]/.test(part)) {
      throw new Error("powershell command parts contain control characters");
    }
    assertNoSecrets("command part", part);
  }
  const program = `& ${command.map(psQuote).join(" ")}`;
  if (program.includes('"')) {
    throw new Error(
      "powershell command parts contain a transport-unsafe double quote"
    );
  }
  return `powershell.exe -NoProfile -NonInteractive -Command "${program}"`;
}

// All git facts arrive as plain values so fixtures stay fast.
// branch: string|null (null = detached HEAD), head/originMain/target: SHAs,
// dirty: boolean, ancestorOk: HEAD is ancestor of target (merge-base).
export function decideUpdate({
  branch,
  head,
  originMain,
  target,
  dirty,
  isLatest,
  ancestorOk,
}) {
  const norm = (v) => String(v ?? "").toLowerCase();
  const t = norm(target);
  if (!SHA_RE.test(t))
    return { ok: false, reason: "bad-target", detail: "target is not a SHA" };
  if (dirty) {
    return {
      ok: false,
      reason: "dirty-checkout",
      detail: `branch=${branch ?? "detached"} head=${norm(head)}`,
    };
  }
  if (isLatest) {
    if (norm(originMain) !== t) {
      return {
        ok: false,
        reason: "revision-mismatch",
        detail: `target != origin/main (${norm(originMain)})`,
      };
    }
    if (norm(head) === t && branch === "main") {
      return { ok: true, action: "already-current", finalBranch: "main" };
    }
    if (norm(head) === t && branch === null) {
      return { ok: true, action: "checkout-main-ff", finalBranch: "main" };
    }
    if (branch !== "main") {
      return {
        ok: false,
        reason: "wrong-branch",
        detail: `branch=${branch ?? "detached"} head=${norm(head)}`,
      };
    }
    if (!ancestorOk) {
      return {
        ok: false,
        reason: "diverged",
        detail: `HEAD ${norm(head)} is not an ancestor of ${t}`,
      };
    }
    return { ok: true, action: "fast-forward", finalBranch: "main" };
  }
  if (norm(head) === t) {
    return {
      ok: true,
      action: "already-current",
      finalBranch: branch ?? "detached",
    };
  }
  return { ok: true, action: "checkout-detached", finalBranch: "detached" };
}

export function combineHostResults(hosts, target) {
  const t = String(target ?? "").toLowerCase();
  const ok = hosts.filter(
    (h) => h?.verdict === "updated" || h?.verdict === "already-current"
  );
  if (ok.length === hosts.length && hosts.length > 0) {
    const same = hosts.every((h) => String(h.head ?? "").toLowerCase() === t);
    if (same) return { verdict: "updated", reason: "updated" };
    return { verdict: "mismatch", reason: "revision-mismatch" };
  }
  const refused = hosts.find((h) => h?.verdict === "refused");
  if (refused)
    return { verdict: "partial", reason: refused.reason ?? "refused" };
  return { verdict: "partial", reason: "host-failed" };
}

// Authenticated Lenovo stdout receipt check for issue #133. The payload is
// the MAC-verified result envelope bound to the requested job id (verified
// inside lan_bridge.py fetch, never here): transport first, receipt
// authoritative. Both completed/ok and error statuses proceed to receipt
// parsing when stdout is complete (the worker seals status error for any
// nonzero process exit, so refused host receipts arrive as error
// envelopes). timed_out is terminal worker timeout (NEW id),
// output_truncated is terminal incomplete (NEW id with larger max-output).
// A verified refused receipt is a verified terminal per-host outcome: it is
// accepted here and reported as partial by the combiner, never as success.
export function verifyLenovoPayload(payload, { target, isLatest }) {
  const fail = (reason, detail = "") => ({
    ok: false,
    reason,
    detail: String(detail).slice(0, 400),
  });
  const t = String(target ?? "").toLowerCase();
  if (!SHA_RE.test(t)) return fail("bad-target", "target is not a SHA");
  if (!payload || typeof payload !== "object")
    return fail("receipt-missing", "no verified payload");
  if (payload.timed_out)
    return fail(
      "worker-timeout",
      "terminal worker timeout: fix limits, then a NEW job id"
    );
  if (payload.output_truncated)
    return fail(
      "output-truncated",
      "terminal incomplete stdout: rerun as a NEW id with larger --max-output"
    );
  if (!["completed", "ok", "error"].includes(payload.status))
    return fail(
      "unknown-status",
      `verified envelope, unrecognized status=${String(payload.status)}`
    );
  const stdout = payload.stdout;
  if (typeof stdout !== "string" || stdout.trim() === "")
    return fail("receipt-missing", "verified envelope carries no stdout");
  let receipt;
  try {
    receipt = JSON.parse(stdout);
  } catch (error) {
    return fail("receipt-unparsable", error.message);
  }
  if (receipt?.schema !== HOST_SCHEMA)
    return fail("receipt-malformed", "bad schema");
  if (!["updated", "already-current", "refused"].includes(receipt.verdict))
    return fail("receipt-malformed", `bad verdict ${receipt.verdict}`);
  try {
    assertNoSecrets("lenovo receipt", stdout);
  } catch (error) {
    return fail("receipt-secret", error.message);
  }
  if (String(receipt.targetSha ?? "").toLowerCase() !== t)
    return fail("revision-mismatch", "receipt target != requested target");
  if (Boolean(receipt.isLatest) !== Boolean(isLatest))
    return fail("revision-mismatch", "receipt isLatest != requested flag");
  if (receipt.verdict === "refused")
    return { ok: true, refused: true, receipt };
  if (String(receipt.head ?? "").toLowerCase() !== t || receipt.clean !== true)
    return fail("revision-mismatch", "receipt head/clean mismatch");
  if (Boolean(isLatest)) {
    if (receipt.branch !== "main")
      return fail("wrong-branch", "latest receipt is not on main");
    if (String(receipt.originMain ?? "").toLowerCase() !== t)
      return fail("revision-mismatch", "latest receipt originMain != target");
  }
  if (receipt.skills?.state !== "current")
    return fail(
      "skills-not-current",
      `skills ${receipt.skills?.state ?? "unknown"}`
    );
  return { ok: true, refused: false, receipt };
}

export const DIAGNOSE_SCHEMA = "two-host-rollout-diagnose/1";

// Read-only classification for issue #161. Pure: facts in, verdict out.
// facts: { branch (string|null; null = detached), head, originMain|null,
//   originReachable, clean (boolean|null; null = unknown), ancestorOk|null,
//   skillsState }. Unknown is never reported as ready: any unproven core
// fact (head, cleanliness) forces unknown.
export function classifyCheckout(facts, { target, isLatest }) {
  const t = String(target ?? "").toLowerCase();
  const nextFor = (reason) =>
    ({
      "dirty-checkout":
        "commit or stash local changes, then rerun the same revision",
      diverged:
        "rebase local commits onto the published revision or pick that revision explicitly; never force",
      detached:
        "move to main (latest) or rerun with --is-latest false for a pinned revision",
      "wrong-branch":
        "move to main (latest) or rerun with --is-latest false for a pinned revision",
      "revision-mismatch":
        "rerun with the verified origin/main SHA, not a guessed one",
      "remote-unavailable":
        "restore the origin remote (network/path), then rerun diagnose; nothing was changed",
      "skills-not-current":
        "remove only foreign or stale skill dirs manually, then rerun; nothing was overwritten",
      "runtime-incomplete":
        "fix the checkout runtime files, then rerun; nothing was changed",
      unknown:
        "stop and reconcile before any retry; unknown is never clean or current",
    })[reason] ?? "stop and reconcile before any retry";
  if (!SHA_RE.test(t))
    return {
      status: "unknown",
      reason: "bad-target",
      nextAction: nextFor("unknown"),
    };
  const head = String(facts?.head ?? "").toLowerCase();
  if (!SHA_RE.test(head) || (facts?.clean !== true && facts?.clean !== false))
    return {
      status: "unknown",
      reason: "unknown",
      nextAction: nextFor("unknown"),
    };
  if (facts.clean !== true)
    return {
      status: "not-ready",
      reason: "dirty-checkout",
      nextAction: nextFor("dirty-checkout"),
    };
  const skills = facts.skillsState ?? "unknown";
  if (skills === "ownership-conflict" || skills === "partial-install")
    return {
      status: "not-ready",
      reason: "skills-not-current",
      nextAction: nextFor("skills-not-current"),
    };
  if (skills === "runtime-incomplete" || skills === "unknown")
    return {
      status: "unknown",
      reason:
        skills === "runtime-incomplete" ? "runtime-incomplete" : "unknown",
      nextAction: nextFor(
        skills === "runtime-incomplete" ? "runtime-incomplete" : "unknown"
      ),
    };
  const branch = facts.branch ?? null;
  const detached = branch === null || branch === "detached";
  // Pinned mode never fetches and apply-host moves any clean checkout with
  // `checkout --detach <target>` (branch refs untouched), so a detached HEAD
  // away from the target is an actionable pinned move, not a blocker.
  // Ancestry and remote reachability stay visible in the entry fields but do
  // not gate pinned readiness, matching the no-fetch pinned rollout path.
  if (detached && !isLatest) {
    if (head === t)
      return {
        status: "ready",
        reason: "ready-detached-pinned",
        nextAction:
          "facts are point-in-time; reverify with apply-host before any authorized rollout",
      };
    return {
      status: "ready",
      reason: "ready-pinned-move",
      nextAction:
        "facts are point-in-time; reverify with apply-host before any authorized rollout",
    };
  }
  if (detached) {
    return {
      status: "not-ready",
      reason: "detached",
      nextAction: nextFor("detached"),
    };
  }
  if (isLatest) {
    if (!facts.originReachable)
      return {
        status: "not-ready",
        reason: "remote-unavailable",
        nextAction: nextFor("remote-unavailable"),
      };
    const originMain = String(facts.originMain ?? "").toLowerCase();
    if (!SHA_RE.test(originMain))
      return {
        status: "unknown",
        reason: "unknown",
        nextAction: nextFor("unknown"),
      };
    if (originMain !== t)
      return {
        status: "not-ready",
        reason: "revision-mismatch",
        nextAction: nextFor("revision-mismatch"),
      };
    if (branch !== "main")
      return {
        status: "not-ready",
        reason: "wrong-branch",
        nextAction: nextFor("wrong-branch"),
      };
    if (head === t)
      return {
        status: "ready",
        reason: "ready-current",
        nextAction:
          "facts are point-in-time; reverify with apply-host before any authorized rollout",
      };
    if (facts.ancestorOk !== true)
      return {
        status: "not-ready",
        reason: "diverged",
        nextAction: nextFor("diverged"),
      };
    return {
      status: "ready",
      reason: "ready-behind",
      nextAction:
        "facts are point-in-time; reverify with apply-host before any authorized rollout",
    };
  }
  if (head === t)
    return {
      status: "ready",
      reason: "ready-pinned",
      nextAction:
        "facts are point-in-time; reverify with apply-host before any authorized rollout",
    };
  return {
    status: "ready",
    reason: "ready-pinned-move",
    nextAction:
      "facts are point-in-time; reverify with apply-host before any authorized rollout",
  };
}

// Deterministic selection over classified candidates for issue #161.
// entries: [{ checkout, classification: { status, reason } }]. Sorted by
// checkout path so the choice never changes silently. One ready entry is a
// justified choice; several ready entries are ambiguous (caller reruns with
// a single --checkout); no ready entry reports the top blocker.
export function selectCheckout(entries, { target, isLatest } = {}) {
  const sorted = [...(entries ?? [])].sort((a, b) =>
    String(a?.checkout ?? "").localeCompare(String(b?.checkout ?? ""))
  );
  const ranking = sorted.map((e) => ({
    checkout: e?.checkout ?? null,
    status: e?.classification?.status ?? "unknown",
    reason: e?.classification?.reason ?? "unknown",
  }));
  const ready = sorted.filter((e) => e?.classification?.status === "ready");
  if (ready.length === 1)
    return {
      choice: ready[0].checkout,
      outcome: "justified-choice",
      reason: ready[0].classification.reason,
      ranking,
      nextAction: `roll out ${ready[0].checkout} only after apply-host reverifies ${String(target ?? "").toLowerCase()} (isLatest=${Boolean(isLatest)}); reset, force and delete are never used`,
    };
  if (ready.length > 1)
    return {
      choice: null,
      outcome: "ambiguous",
      reason: "ambiguous",
      ranking,
      nextAction: `more than one ready checkout (${ready.length}): rerun diagnose with a single --checkout for the intended copy; nothing was changed`,
    };
  const first = sorted[0];
  return {
    choice: null,
    outcome: "no-ready-candidate",
    reason: first?.classification?.reason ?? "unknown",
    ranking,
    nextAction:
      first?.classification?.nextAction ??
      "stop and reconcile before any retry",
  };
}
