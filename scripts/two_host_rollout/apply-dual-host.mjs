#!/usr/bin/env node
// One-host dual-repo rollout step for issue #162. Runs locally on Dell and,
// via the signed queue, on Lenovo with identical argv. Reuses each repo's
// native installer and evidence scheme, never a second implementation:
//
// - task-runner: scripts/two_host_rollout/apply-host.mjs (same argv for the
//   TR pin; receipt schema two-host-rollout-host/1). Foreign installs,
//   dirty/diverged checkouts and divergent commits are never overwritten,
//   no destructive git reset is attempted.
// - PAT: <pat-checkout>/bootstrap/rollout.ps1 (observed upstream contract:
//   -Revision <sha> -InstalledCheckout <dir> -SkillsDirectory <dir>
//   [-CodexDirectory/-OpenCodeDirectory/-PracticesDirectory] -ReceiptPath;
//   receipt schema personal-agent-toolkit/skill-rollout@1). Dirty PAT trees
//   are handled inside that installer; this script never forces them.
//
// Order is TR then PAT. A TR pre-mutation refusal leaves PAT untouched; a
// post-TR PAT refusal reports the moved TR HEAD honestly with no rollback,
// exactly like the single-repo path. Rerun of the same two revisions is
// safe: both installers detect already-current and change nothing.
// Secrets never enter argv or receipts. --pat-rollout-script is test-only:
// a .mjs fixture stub executed with node instead of the native ps1.
import { execFileSync, spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DUAL_HOST_SCHEMA, SHA_RE, verifyDualReceipt } from "./dual-lib.mjs";
import { assertNoSecrets } from "./lib.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");
const trApply = join(repoRoot, "scripts", "two_host_rollout", "apply-host.mjs");

function usage(message) {
  process.stderr.write(`two-host-rollout apply-dual-host: ${message}\n`);
  process.stderr.write(
    "usage: apply-dual-host.mjs --checkout <dir> --target-sha <sha> --is-latest true|false --skills-dir <dir> --pat-checkout <dir> --pat-sha <sha> --pat-skills-dir <dir> --receipt <file> [--pat-codex-dir <dir> --pat-opencode-dir <dir> --pat-practices-dir <dir> --pat-rollout-script <file> --origin-ref <ref> --host-label dell|lenovo]\n"
  );
  process.exit(2);
}

const raw = process.argv.slice(2);
const opts = {};
for (let i = 0; i < raw.length; i += 2) {
  const key = raw[i];
  const value = raw[i + 1];
  if (
    ![
      "--checkout",
      "--target-sha",
      "--is-latest",
      "--skills-dir",
      "--pat-checkout",
      "--pat-sha",
      "--pat-skills-dir",
      "--receipt",
      "--pat-codex-dir",
      "--pat-opencode-dir",
      "--pat-practices-dir",
      "--pat-rollout-script",
      "--origin-ref",
      "--host-label",
    ].includes(key)
  ) {
    usage(`unknown argument: ${key}`);
  }
  if (value === undefined || value.startsWith("--")) usage(`${key} requires a value`);
  opts[key] = value;
}
for (const key of [
  "--checkout",
  "--target-sha",
  "--is-latest",
  "--skills-dir",
  "--pat-checkout",
  "--pat-sha",
  "--pat-skills-dir",
  "--receipt",
]) {
  if (opts[key] === undefined) usage(`${key} is required`);
}
const checkout = resolve(opts["--checkout"]);
const skillsDir = resolve(opts["--skills-dir"]);
const patCheckout = resolve(opts["--pat-checkout"]);
const patSkillsDir = resolve(opts["--pat-skills-dir"]);
const receiptPath = resolve(opts["--receipt"]);
const originRef = opts["--origin-ref"] ?? "refs/remotes/origin/main";
const hostLabel = opts["--host-label"] ?? "unknown";
if (!["dell", "lenovo", "unknown"].includes(hostLabel)) usage("--host-label must be dell|lenovo|unknown");
const isLatest =
  opts["--is-latest"] === "true"
    ? true
    : opts["--is-latest"] === "false"
      ? false
      : usage("--is-latest must be true|false");
const trt = String(opts["--target-sha"]).toLowerCase();
const pt = String(opts["--pat-sha"]).toLowerCase();
if (!SHA_RE.test(trt)) usage("--target-sha must be a full commit SHA");
if (!SHA_RE.test(pt)) usage("--pat-sha must be a full commit SHA");
for (const [label, value] of [
  ["--checkout", checkout],
  ["--skills-dir", skillsDir],
  ["--pat-checkout", patCheckout],
  ["--pat-skills-dir", patSkillsDir],
]) {
  if (!isAbsolute(value)) usage(`${label} must be absolute`);
}
try {
  assertNoSecrets("argv", raw.join(" "));
} catch (error) {
  usage(error.message);
}

const receipt = {
  schema: DUAL_HOST_SCHEMA,
  verdict: null,
  reason: null,
  detail: "",
  host: hostLabel,
  taskRunnerTarget: trt,
  patTarget: pt,
  isLatest,
  checkout,
  patCheckout,
  taskRunner: null,
  pat: null,
  nextAction: null,
};

function gitReadback(dir) {
  const out = { head: null, clean: null, branch: null };
  try {
    out.head = execFileSync("git", ["rev-parse", "--verify", "HEAD"], { cwd: dir, encoding: "utf8" }).trim().toLowerCase();
  } catch { /* keep null */ }
  try {
    out.clean = execFileSync("git", ["status", "--porcelain"], { cwd: dir, encoding: "utf8" }).trim() === "";
  } catch { /* keep null */ }
  try {
    out.branch = execFileSync("git", ["symbolic-ref", "--short", "HEAD"], { cwd: dir, encoding: "utf8" }).trim();
  } catch {
    out.branch = "detached";
  }
  return out;
}

function finish(code, verdict, reason, detail = "") {
  receipt.verdict = verdict;
  receipt.reason = reason;
  receipt.detail = String(detail).slice(0, 400);
  receipt.nextAction =
    {
      updated: "both dual-host receipts verify against the same two pins; see docs/two-host-dual-rollout.md",
      "already-current": "no change on either repo; rerun is safe",
      refused: "fix per sub-receipt nextAction, then rerun the same two revisions",
    }[verdict] ?? "stop and reconcile before any retry";
  try {
    assertNoSecrets("receipt", JSON.stringify(receipt));
  } catch (error) {
    process.stderr.write(`refusing secret-bearing receipt: ${error.message}\n`);
    process.exit(2);
  }
  mkdirSync(dirname(receiptPath), { recursive: true });
  writeFileSync(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(JSON.stringify(receipt));
  process.exit(code);
}

// 1. Task-runner half through its native installer. A temp receipt keeps the
// caller's --receipt path exclusively for the dual receipt.
const tmp = mkdtempSync(join(tmpdir(), "dual-rollout-"));
const trReceiptPath = join(tmp, "tr.receipt.json");
{
  const r = spawnSync(
    process.execPath,
    [
      trApply,
      "--checkout", checkout,
      "--target-sha", trt,
      "--is-latest", String(isLatest),
      "--skills-dir", skillsDir,
      "--receipt", trReceiptPath,
      "--origin-ref", originRef,
    ],
    { encoding: "utf8" }
  );
  let parsed = null;
  try {
    parsed = JSON.parse(readFileSync(trReceiptPath, "utf8"));
  } catch {
    finish(1, "refused", "install-failed", `task-runner installer produced no receipt (exit ${r.status})`);
  }
  receipt.taskRunner = parsed;
  if (r.status !== 0 || !["updated", "already-current"].includes(parsed?.verdict)) {
    const rb = gitReadback(checkout);
    finish(
      1,
      "refused",
      parsed?.reason ?? "install-failed",
      `task-runner refused; PAT untouched; tr branch=${rb.branch} head=${rb.head} clean=${rb.clean}`
    );
  }
}

// 2. PAT half through its native rollout. Default is the real installer path
// inside the PAT checkout; --pat-rollout-script replaces it only for
// offline fixtures (a .mjs stub run with node).
const patReceiptPath = join(tmp, "pat.receipt.json");
{
  const override = opts["--pat-rollout-script"];
  let status = null;
  let stderr = "";
  if (override) {
    const script = resolve(override);
    const r = spawnSync(
      process.execPath,
      [script, "--pat-checkout", patCheckout, "--pat-sha", pt, "--pat-skills-dir", patSkillsDir, "--receipt", patReceiptPath],
      { encoding: "utf8" }
    );
    status = r.status;
    stderr = `${r.stderr ?? ""}`;
  } else {
    const installer = join(patCheckout, "bootstrap", "rollout.ps1");
    const args = [
      "-NoProfile",
      "-NonInteractive",
      "-File",
      installer,
      "-Revision", pt,
      "-InstalledCheckout", patCheckout,
      "-SkillsDirectory", patSkillsDir,
      "-ReceiptPath", patReceiptPath,
    ];
    if (opts["--pat-codex-dir"]) args.push("-CodexDirectory", resolve(opts["--pat-codex-dir"]));
    if (opts["--pat-opencode-dir"]) args.push("-OpenCodeDirectory", resolve(opts["--pat-opencode-dir"]));
    if (opts["--pat-practices-dir"]) args.push("-PracticesDirectory", resolve(opts["--pat-practices-dir"]));
    let r = spawnSync("powershell.exe", args, { encoding: "utf8", timeout: 600000 });
    if (r.error?.code === "ENOENT") {
      r = spawnSync("pwsh", args, { encoding: "utf8", timeout: 600000 });
    }
    if (r.error) {
      const trb = gitReadback(checkout);
      finish(1, "refused", "pat-missing", `PAT powershell unavailable: ${r.error.message}; tr head=${trb.head}`);
    }
    status = r.status;
    stderr = `${r.stderr ?? ""}`;
  }
  let parsed = null;
  try {
    parsed = JSON.parse(readFileSync(patReceiptPath, "utf8"));
  } catch {
    const trb = gitReadback(checkout);
    finish(1, "refused", "pat-install-failed", `PAT installer produced no receipt (exit ${status}): ${stderr.slice(0, 200)}; tr head=${trb.head} (already moved, no rollback)`);
  }
  receipt.pat = parsed;
  if (status !== 0 || parsed?.ok !== true) {
    const trb = gitReadback(checkout);
    const patb = gitReadback(patCheckout);
    finish(
      1,
      "refused",
      "pat-install-failed",
      `${parsed?.failure?.reason ?? "pat refused"}: ${parsed?.failure?.message ?? stderr.slice(0, 150)}; tr head=${trb.head} pat head=${patb.head} (no rollback)`
    );
  }
}

// 3. Both halves claim success: verify the bundle against both pins before
// declaring anything. A verification failure here is refused, never updated.
{
  receipt.verdict = "updated";
  receipt.reason = "updated";
  const checked = verifyDualReceipt(receipt, { taskRunnerTarget: trt, patTarget: pt, isLatest });
  if (!checked.ok) {
    finish(1, "refused", checked.reason, checked.detail);
  }
  if (checked.refused) {
    finish(1, "refused", checked.reason ?? "host-failed", checked.detail ?? "");
  }
  const verdict =
    receipt.taskRunner?.verdict === "already-current" && receipt.pat?.mutation === "none"
      ? "already-current"
      : "updated";
  // PAT has no already-current verdict: combined already-current needs TR
  // already-current AND a PAT run that changed nothing (mutation none).
  finish(0, verdict, verdict);
}
