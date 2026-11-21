import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const requestDual = join(repoRoot, "scripts", "two_host_rollout", "request-dual-rollout.mjs");
const verifyDual = join(repoRoot, "scripts", "two_host_rollout", "verify-dual-readback.mjs");
const TRT = "a".repeat(40);
const PAT = "b".repeat(40);

function cli(script, args) {
  try {
    return {
      code: 0,
      out: execFileSync("node", [script, ...args], { encoding: "utf8" }),
    };
  } catch (error) {
    return {
      code: error.status ?? 1,
      out: `${error.stdout ?? ""}${error.stderr ?? ""}`,
    };
  }
}

function dualDoc(verdict = "updated", reason = "updated") {
  return {
    schema: "two-host-dual-host/1",
    verdict,
    reason,
    taskRunnerTarget: TRT,
    patTarget: PAT,
    isLatest: true,
    checkout: "C:\\tr",
    patCheckout: "C:\\pat",
    taskRunner: {
      schema: "two-host-rollout-host/1",
      verdict: "updated",
      reason: "updated",
      targetSha: TRT,
      isLatest: true,
      branch: "main",
      head: TRT,
      originMain: TRT,
      clean: true,
      skills: { state: "current" },
    },
    pat: {
      schema: "personal-agent-toolkit/skill-rollout@1",
      ok: true,
      pinnedRevision: PAT,
      installedRevision: PAT,
      installedCheckout: "C:\\pat",
      verification: {
        installedCheckoutHead: PAT,
        verified: true,
        verificationPasses: 1,
        checkoutClean: true,
        junctions: { mismatched: [] },
        verificationErrors: [],
        installProblems: [],
      },
    },
  };
}

function baseArgs(root, shared, commandFile) {
  return [
    "--node", "C:\\lenovo\\node.exe",
    "--lenovo-checkout", "C:\\lenovo\\task-runner",
    "--lenovo-skills-dir", "C:\\lenovo\\skills",
    "--lenovo-pat-checkout", "C:\\lenovo\\pat",
    "--lenovo-pat-skills-dir", "C:\\lenovo\\pat-skills",
    "--task-runner-sha", TRT,
    "--pat-sha", PAT,
    "--is-latest", "true",
    "--request-name", "dual.json",
    "--command-file", commandFile,
    "--shared", shared,
  ];
}

test("request builder pins two SHAs in one secret-free Lenovo job", () => {
  const root = mkdtempSync(join(tmpdir(), "dual-req-"));
  const shared = join(root, "share");
  mkdirSync(shared, { recursive: true });
  const commandFile = join(root, "cmd.txt");
  const r = cli(requestDual, baseArgs(root, shared, commandFile));
  assert.equal(r.code, 0, r.out);
  const built = JSON.parse(r.out);
  assert.equal(built.requestSha256.length, 64);
  const stored = JSON.parse(readFileSync(join(shared, "dual.json"), "utf8"));
  assert.equal(stored.schema, "two-host-dual-rollout-request/1");
  assert.equal(stored.taskRunnerSha, TRT);
  assert.equal(stored.patSha, PAT);
  const command = readFileSync(commandFile, "utf8");
  assert.match(command, /apply-dual-host\.mjs/);
  assert.match(command, new RegExp(TRT));
  assert.match(command, new RegExp(PAT));
  assert.doesNotMatch(command, /JIRA_SESSION_COOKIE|DCO_.*TOKEN/);
});

test("request builder refuses a bad second pin", () => {
  const root = mkdtempSync(join(tmpdir(), "dual-req-"));
  mkdirSync(join(root, "share"), { recursive: true });
  const bad = cli(requestDual, baseArgs(root, join(root, "share"), join(root, "cmd.txt")).map((v, i, a) =>
    a[i - 1] === "--pat-sha" ? "zzz" : v
  ));
  assert.notEqual(bad.code, 0);
});

test("verify accepts four verified combos and rerun is identical", () => {
  const root = mkdtempSync(join(tmpdir(), "dual-verify-"));
  const dell = join(root, "dell.json");
  const lenovo = join(root, "lenovo.json");
  writeFileSync(dell, JSON.stringify(dualDoc()));
  writeFileSync(lenovo, JSON.stringify(dualDoc()));
  const args = ["--dell-receipt", dell, "--lenovo-receipt", lenovo, "--task-runner-sha", TRT, "--pat-sha", PAT, "--is-latest", "true"];
  const first = cli(verifyDual, args);
  const second = cli(verifyDual, args);
  assert.equal(first.code, 0, first.out);
  assert.equal(second.code, 0, second.out);
  assert.equal(JSON.parse(first.out).verdict, "updated");
  assert.equal(first.out, second.out, "idempotent rerun: same pins give the same receipt");
});

test("verify reports partial on one refused host and mismatch on drift", () => {
  const root = mkdtempSync(join(tmpdir(), "dual-verify-"));
  const dell = join(root, "dell.json");
  const lenovo = join(root, "lenovo.json");
  writeFileSync(dell, JSON.stringify(dualDoc("refused", "dirty-checkout")));
  writeFileSync(lenovo, JSON.stringify(dualDoc()));
  const partial = cli(verifyDual, ["--dell-receipt", dell, "--lenovo-receipt", lenovo, "--task-runner-sha", TRT, "--pat-sha", PAT, "--is-latest", "true"]);
  assert.notEqual(partial.code, 0);
  assert.equal(JSON.parse(partial.out).verdict, "partial");
  // Drifted PAT pin on one host is never updated.
  writeFileSync(dell, JSON.stringify(dualDoc()));
  const drift = cli(verifyDual, ["--dell-receipt", dell, "--lenovo-receipt", lenovo, "--task-runner-sha", TRT, "--pat-sha", "c".repeat(40), "--is-latest", "true"]);
  assert.notEqual(drift.code, 0);
  assert.notEqual(JSON.parse(drift.out).verdict, "updated");
});
