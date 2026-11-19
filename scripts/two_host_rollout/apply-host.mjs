#!/usr/bin/env node
// One-host rollout step for issue #133. Runs locally on Dell and, via the
// signed queue, on Lenovo with the identical argv.
// When --is-latest true the script runs `git fetch -q origin` itself with the
// host git credentials and requires target == fetched origin/main: freshness
// is proven by that authenticated fetch/readback, never by trusting the
// caller flag. A pinned older revision (--is-latest false) never fetches for
// freshness and never claims to be latest.
// Latest main: clean checkout, main ancestry, finish on branch main with
// HEAD == origin/main == target, no detached HEAD. Older pinned revision:
// clean checkout, detached HEAD at target, main ref untouched, never declared
// current.
// Ownership is preflighted against the working tree before any checkout
// mutation (same bytes the installer hashes), the target blobs are proven
// present first, and ownership is revalidated immediately after mutation;
// local changes, foreign installs, and divergent commits are never
// overwritten and no destructive git reset is attempted. Post-mutation
// install failures report the new HEAD readback instead of hiding the
// mutation. Secrets never enter argv or receipts.
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { HOST_SCHEMA, assertNoSecrets, decideUpdate } from "./lib.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");
const installer = join(repoRoot, "scripts", "install-cli-jira-skills.mjs");

const RUNTIME_FILES = [
  "scripts/jira-protocol.mjs",
  "scripts/jira_protocol/protocol.mjs",
  "scripts/jira_protocol/operations.mjs",
  "scripts/jira_protocol/operations-shared.mjs",
  "scripts/jira_protocol/operations-transition.mjs",
  "scripts/jira_protocol/operations-assign.mjs",
  "scripts/jira_protocol/operations-epic.mjs",
  "scripts/jira_protocol/operations-subtask.mjs",
  "scripts/jira_protocol/operations-flag.mjs",
  "scripts/jira_protocol/route.mjs",
  "scripts/jira_protocol/batch.mjs",
  "scripts/jira_protocol/batch-parse.mjs",
  "scripts/jira_protocol/batch-journal.mjs",
  "scripts/jira_protocol/batch-plan.mjs",
  "scripts/jira_protocol/batch-execute.mjs",
  "scripts/jira_protocol/handoff.mjs",
  "scripts/jira_protocol/extract.mjs",
  "scripts/jira_protocol/html-form.mjs",
  "docs/jira-protocol.md",
  "docs/jira-protocol-subtasks.md",
  "docs/jira-protocol-batch.md",
  "docs/jira-protocol-handoff.md",
];

function usage(message) {
  process.stderr.write(`two-host-rollout apply-host: ${message}\n`);
  process.stderr.write(
    "usage: apply-host.mjs --checkout <dir> --target-sha <sha> --is-latest true|false --skills-dir <dir> --receipt <file> [--origin-ref refs/remotes/origin/main]\n"
  );
  process.exit(2);
}

function parse(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const value = argv[i + 1];
    if (
      ![
        "--checkout",
        "--target-sha",
        "--is-latest",
        "--skills-dir",
        "--receipt",
        "--origin-ref",
      ].includes(key)
    ) {
      usage(`unknown argument: ${key}`);
    }
    if (value === undefined || value.startsWith("--"))
      usage(`${key} requires a value`);
    out[key] = value;
    i += 1;
  }
  for (const key of [
    "--checkout",
    "--target-sha",
    "--is-latest",
    "--skills-dir",
    "--receipt",
  ]) {
    if (out[key] === undefined) usage(`${key} is required`);
  }
  return out;
}

const opts = parse(process.argv.slice(2));
const checkout = resolve(opts["--checkout"]);
const skillsDir = resolve(opts["--skills-dir"]);
const receiptPath = resolve(opts["--receipt"]);
const originRef = opts["--origin-ref"] ?? "refs/remotes/origin/main";
const isLatest =
  opts["--is-latest"] === "true"
    ? true
    : opts["--is-latest"] === "false"
      ? false
      : usage("--is-latest must be true|false");
const target = String(opts["--target-sha"]).toLowerCase();

for (const [label, value] of [
  ["--checkout", checkout],
  ["--skills-dir", skillsDir],
]) {
  if (!isAbsolute(value)) usage(`${label} must be absolute`);
}
try {
  assertNoSecrets("argv", process.argv.slice(2).join(" "));
} catch (error) {
  usage(error.message);
}

const receipt = {
  schema: HOST_SCHEMA,
  verdict: null,
  reason: null,
  detail: "",
  checkout,
  targetSha: target,
  isLatest,
  branch: null,
  head: null,
  originMain: null,
  clean: null,
  skills: { dir: skillsDir, state: null, files: [] },
  nextAction: null,
};

function git(args) {
  return execFileSync("git", args, { cwd: checkout, encoding: "utf8" }).trim();
}

function gitBytes(args) {
  return execFileSync("git", args, { cwd: checkout });
}

function refreshGitReadback() {
  try {
    receipt.branch = (() => {
      try {
        return git(["symbolic-ref", "--short", "HEAD"]);
      } catch {
        return "detached";
      }
    })();
  } catch {
    receipt.branch = "detached";
  }
  try {
    receipt.head = git(["rev-parse", "--verify", "HEAD"]).toLowerCase();
  } catch {
    /* keep previous head on read failure */
  }
  try {
    receipt.clean = git(["status", "--porcelain"]) === "";
  } catch {
    /* keep previous clean flag */
  }
  try {
    receipt.originMain = git(["rev-parse", "--verify", originRef]).toLowerCase();
  } catch {
    /* keep previous originMain */
  }
}

function finishWithFreshReadback(code, verdict, reason, detail = "") {
  refreshGitReadback();
  finish(code, verdict, reason, `${detail}; branch=${receipt.branch} head=${receipt.head} clean=${receipt.clean}`.slice(0, 400));
}

function finish(code, verdict, reason, detail = "") {
  receipt.verdict = verdict;
  receipt.reason = reason;
  receipt.detail = String(detail).slice(0, 400);
  receipt.nextAction =
    {
      updated:
        "both host receipts verify against the same target; see docs/two-host-rollout.md",
      "already-current": "no change; rerun is safe",
      "dirty-checkout":
        "commit or stash local changes, then rerun the same revision",
      diverged:
        "rebase local commits onto the published revision or pick that revision explicitly; never force",
      "wrong-branch":
        "move to main (latest) or rerun with --is-latest false for a pinned revision",
      "revision-mismatch":
        "rerun with the verified origin/main SHA, not a guessed one",
      "ownership-conflict":
        "remove only foreign or stale skill dirs manually, then rerun; nothing was overwritten",
      "partial-install":
        "complete the interrupted install manually or clear the skills dir, then rerun",
      "install-failed":
        "read installer stderr, fix the runtime checkout, then rerun",
      "git-failed": "fix the checkout and rerun the same revision",
    }[reason] ?? "stop and reconcile before any retry";
  assertNoSecrets("receipt", JSON.stringify(receipt));
  mkdirSync(dirname(receiptPath), { recursive: true });
  writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(JSON.stringify(receipt));
  process.exit(code);
}

function freshHashes() {
  const hashes = {};
  for (const relative of RUNTIME_FILES) {
    hashes[relative] = createHash("sha256")
      .update(readFileSync(join(checkout, relative)))
      .digest("hex");
  }
  return hashes;
}

function targetHashes() {
  const hashes = {};
  for (const relative of RUNTIME_FILES) {
    let bytes;
    try {
      bytes = gitBytes(["show", `${target}:${relative}`]);
    } catch {
      throw new Error(`target is missing runtime file: ${relative}`);
    }
    hashes[relative] = createHash("sha256").update(bytes).digest("hex");
  }
  return hashes;
}

const SKILL_NAMES = [
  "cli-create-lel-service-backlog-issue",
  "cli-assign-jira-issue",
  "cli-transition-jira-issue",
  "cli-transition-jira-status",
  "cli-extract-jira-content",
  "cli-flag-jira-issue",
  "cli-create-jira-subtask",
  "cli-create-jira-subtasks",
];

function assessSkills(fresh) {
  const names = SKILL_NAMES;
  if (!fresh) return { state: "runtime-incomplete", names, fresh: null };
  const states = [];
  for (const name of names) {
    const dir = join(skillsDir, name);
    if (!existsSync(dir)) {
      states.push("missing");
      continue;
    }
    let manifest;
    try {
      manifest = JSON.parse(
        readFileSync(join(dir, "runtime-manifest.json"), "utf8")
      );
    } catch {
      return {
        state: "ownership-conflict",
        names,
        fresh,
        detail: `${name}: no managed manifest`,
      };
    }
    const expected = { ...fresh };
    expected["references/jira-protocol.md"] = fresh["docs/jira-protocol.md"];
    expected["references/jira-protocol-subtasks.md"] =
      fresh["docs/jira-protocol-subtasks.md"];
    expected["references/jira-protocol-batch.md"] =
      fresh["docs/jira-protocol-batch.md"];
    expected["references/jira-protocol-handoff.md"] =
      fresh["docs/jira-protocol-handoff.md"];
    delete expected["docs/jira-protocol.md"];
    delete expected["docs/jira-protocol-subtasks.md"];
    delete expected["docs/jira-protocol-batch.md"];
    delete expected["docs/jira-protocol-handoff.md"];
    const keys = Object.keys(expected);
    if (
      manifest.schema !== "cli-jira-skill-runtime/1" ||
      keys.some((k) => manifest.files?.[k] !== expected[k])
    ) {
      return {
        state: "ownership-conflict",
        names,
        fresh,
        detail: `${name}: foreign or stale install`,
      };
    }
    states.push("current");
  }
  if (states.every((s) => s === "current"))
    return { state: "current", names, fresh };
  if (states.every((s) => s === "missing"))
    return { state: "missing", names, fresh };
  return {
    state: "partial-install",
    names,
    fresh,
    detail: "some skills installed, some missing",
  };
}

let branch = null;
let head = null;
let originMain = null;
let clean = null;
let ancestorOk = false;
try {
  try {
    branch = git(["symbolic-ref", "--short", "HEAD"]);
  } catch {
    branch = null;
  }
  head = git(["rev-parse", "--verify", "HEAD"]).toLowerCase();
  originMain = git(["rev-parse", "--verify", originRef]).toLowerCase();
  clean = git(["status", "--porcelain"]) === "";
  try {
    execFileSync("git", ["merge-base", "--is-ancestor", head, target], {
      cwd: checkout,
    });
    ancestorOk = true;
  } catch {
    ancestorOk = head === target;
  }
} catch (error) {
  finish(1, "refused", "git-failed", error.message);
}
// Freshness is enforced here by an authenticated fetch, never by trusting
// the caller flag alone: when latest is requested the local origin/main ref
// is refreshed from origin first, then target must equal the fetched tip.
if (isLatest) {
  try {
    execFileSync("git", ["fetch", "-q", "origin"], {
      cwd: checkout,
      encoding: "utf8",
    });
  } catch (error) {
    finish(1, "refused", "git-failed", `fetch origin failed: ${error.message}`);
  }
  try {
    originMain = git(["rev-parse", "--verify", originRef]).toLowerCase();
  } catch (error) {
    finish(1, "refused", "git-failed", error.message);
  }
  // The target object may have arrived only via this fetch: recompute
  // ancestry after the fetch instead of trusting the pre-fetch probe.
  try {
    execFileSync("git", ["merge-base", "--is-ancestor", head, target], {
      cwd: checkout,
    });
    ancestorOk = true;
  } catch {
    ancestorOk = head === target;
  }
}
receipt.branch = branch ?? "detached";
receipt.head = head;
receipt.originMain = originMain;
receipt.clean = clean;

// Target must carry the runtime: read the target blobs without touching the
// checkout. A target missing runtime refuses before any mutation.
try {
  targetHashes();
} catch (error) {
  finish(1, "refused", "git-failed", error.message);
}

// Preflight ownership against the current checkout BEFORE any mutation.
// Foreign (no manifest) or partially installed skills refuse here with the
// checkout untouched. Hashes come from the working tree (the same bytes the
// installer hashes), so CRLF-normalized checkouts do not false-positive:
// a skill current for the old runtime but stale for the new target passes
// here and is caught by the post-mutation revalidation with the new HEAD
// visible. Missing skills proceed to mutation then install.
let currentFresh;
try {
  currentFresh = freshHashes();
} catch {
  finish(1, "refused", "git-failed", "runtime incomplete before update");
}
const preflight = assessSkills(currentFresh);
if (
  preflight.state === "ownership-conflict" ||
  preflight.state === "partial-install"
) {
  finish(
    1,
    "refused",
    preflight.state,
    `${preflight.detail ?? "skills dir is not managed"}; branch=${branch ?? "detached"} head=${head} clean=${clean} (no checkout change)`
  );
}

const plan = decideUpdate({
  branch,
  head,
  originMain,
  target,
  dirty: !clean,
  isLatest,
  ancestorOk,
});
if (!plan.ok)
  finish(
    1,
    "refused",
    plan.reason,
    `${plan.detail}; branch=${branch ?? "detached"} head=${head} clean=${clean}`
  );

try {
  if (plan.action === "fast-forward") {
    execFileSync("git", ["merge", "--ff-only", target], {
      cwd: checkout,
      encoding: "utf8",
    });
  } else if (plan.action === "checkout-main-ff") {
    execFileSync("git", ["checkout", "-q", "main"], {
      cwd: checkout,
      encoding: "utf8",
    });
    execFileSync("git", ["merge", "--ff-only", target], {
      cwd: checkout,
      encoding: "utf8",
    });
  } else if (plan.action === "checkout-detached") {
    execFileSync("git", ["checkout", "-q", "--detach", target], {
      cwd: checkout,
      encoding: "utf8",
    });
  }
} catch (error) {
  // A failed mutation may still have moved HEAD (e.g. checkout-main-ff
  // checked out main before merge failed): report the actual readback,
  // never a destructive reset.
  finishWithFreshReadback(
    1,
    "refused",
    isLatest ? "diverged" : "git-failed",
    String(error.message).slice(0, 300)
  );
}

// Revalidate immediately after the checkout mutation: the target runtime
// may differ from the pre-mutation checkout, so a stale or foreign install
// that passed no earlier check refuses here with the new HEAD visible.
let postFresh;
try {
  postFresh = freshHashes();
} catch {
  finishWithFreshReadback(1, "refused", "git-failed", "runtime incomplete after update");
}
const postCheck = assessSkills(postFresh);
if (
  postCheck.state === "ownership-conflict" ||
  postCheck.state === "partial-install" ||
  postCheck.state === "runtime-incomplete"
) {
  finishWithFreshReadback(
    1,
    "refused",
    postCheck.state === "runtime-incomplete" ? "git-failed" : postCheck.state,
    postCheck.detail ?? "skills dir is not managed"
  );
}
if (postCheck.state === "missing") {
  mkdirSync(skillsDir, { recursive: true });
  const run = spawnSync(
    process.execPath,
    [installer, "--target-root", skillsDir, "--runtime-root", checkout],
    { encoding: "utf8" }
  );
  if (run.status !== 0) {
    // The checkout already moved: surface the new HEAD, do not reset it
    // away. Rerun of the same revision is safe after fixing the installer
    // input; no automatic rollback is attempted.
    finishWithFreshReadback(
      1,
      "refused",
      "install-failed",
      `${run.stderr ?? ""}${run.stdout ?? ""}`.slice(0, 300)
    );
  }
}
for (const entry of readdirSync(skillsDir)) {
  if (entry.includes(".install-") || entry.endsWith(".bak")) {
    rmSync(join(skillsDir, entry), { recursive: true, force: true });
  }
}
const leftovers = readdirSync(skillsDir).filter(
  (e) => e.includes(".install-") || e.endsWith(".bak")
);
if (leftovers.length > 0)
  finishWithFreshReadback(
    1,
    "refused",
    "install-failed",
    `backups remain: ${leftovers.join(",")}`
  );

try {
  try {
    receipt.branch = git(["symbolic-ref", "--short", "HEAD"]);
  } catch {
    receipt.branch = "detached";
  }
  receipt.head = git(["rev-parse", "--verify", "HEAD"]).toLowerCase();
  receipt.clean = git(["status", "--porcelain"]) === "";
  receipt.originMain = git(["rev-parse", "--verify", originRef]).toLowerCase();
  if (
    isLatest &&
    (receipt.branch !== "main" ||
      receipt.head !== target ||
      receipt.originMain !== target ||
      !receipt.clean)
  ) {
    finish(
      1,
      "refused",
      "revision-mismatch",
      `after update: branch=${receipt.branch} head=${receipt.head} originMain=${receipt.originMain} clean=${receipt.clean}`
    );
  }
  if (!isLatest && (receipt.head !== target || !receipt.clean)) {
    finish(
      1,
      "refused",
      "revision-mismatch",
      `after update: branch=${receipt.branch} head=${receipt.head} clean=${receipt.clean}`
    );
  }
  let finalFresh;
  try {
    finalFresh = freshHashes();
  } catch {
    finish(1, "refused", "git-failed", "runtime incomplete after update");
  }
  const final = assessSkills(finalFresh);
  if (final.state !== "current")
    finishWithFreshReadback(
      1,
      "refused",
      "install-failed",
      `skills not current after install: ${final.state}`
    );
  receipt.skills = { dir: skillsDir, state: "current", files: SKILL_NAMES };
} catch (error) {
  finish(1, "refused", "git-failed", error.message);
}
finish(
  0,
  plan.action === "already-current" ? "already-current" : "updated",
  plan.action === "already-current" ? "already-current" : "updated"
);
