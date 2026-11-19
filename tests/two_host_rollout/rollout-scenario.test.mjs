import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const requestRollout = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "request-rollout.mjs"
);
const verifyReadback = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "verify-readback.mjs"
);
const target = "a".repeat(40);

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

test("request builder writes a secret-free powershell job for the signed queue", () => {
  const root = mkdtempSync(join(tmpdir(), "two-host-req-"));
  const shared = join(root, "share");
  mkdirSync(shared, { recursive: true });
  const commandFile = join(root, "cmd.txt");
  const r = cli(requestRollout, [
    "--node",
    "C:\\lenovo\\node.exe",
    "--lenovo-checkout",
    "C:\\lenovo\\task-runner",
    "--lenovo-skills-dir",
    "C:\\lenovo\\skills",
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--request-name",
    "rollout.json",
    "--command-file",
    commandFile,
    "--shared",
    shared,
  ]);
  assert.equal(r.code, 0, r.out);
  const built = JSON.parse(r.out);
  assert.equal(built.requestSha256.length, 64);
  const stored = JSON.parse(readFileSync(join(shared, "rollout.json"), "utf8"));
  assert.equal(stored.schema, "two-host-rollout-request/1");
  assert.equal(stored.targetSha, target);
  const command = readFileSync(commandFile, "utf8");
  assert.match(
    command,
    /^powershell\.exe -NoProfile -NonInteractive -Command "& '/
  );
  assert.match(command, new RegExp(target));
  assert.doesNotMatch(command, /JIRA_SESSION_COOKIE|DCO_.*TOKEN/);
});

test("request builder refuses secrets and unsafe command parts", () => {
  const root = mkdtempSync(join(tmpdir(), "two-host-req-"));
  const bad = cli(requestRollout, [
    "--node",
    "C:\\lenovo\\node.exe",
    "--lenovo-checkout",
    "C:\\lenovo\\task-runner",
    "--lenovo-skills-dir",
    "C:\\lenovo\\JIRA_SESSION_COOKIE=x",
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--request-name",
    "rollout.json",
    "--command-file",
    join(root, "cmd.txt"),
    "--shared",
    root,
  ]);
  assert.notEqual(bad.code, 0);
});

test("verify-readback accepts a matching pair and rejects drift", () => {
  const root = mkdtempSync(join(tmpdir(), "two-host-verify-"));
  const host = (branch) => ({
    schema: "two-host-rollout-host/1",
    verdict: "updated",
    targetSha: target,
    isLatest: true,
    branch,
    head: target,
    originMain: target,
    clean: true,
    skills: { state: "current" },
  });
  const dell = join(root, "dell.json");
  const lenovo = join(root, "lenovo.json");
  writeFileSync(dell, JSON.stringify(host("main")));
  writeFileSync(lenovo, JSON.stringify(host("main")));
  const ok = cli(verifyReadback, [
    "--dell-receipt",
    dell,
    "--lenovo-receipt",
    lenovo,
    "--target-sha",
    target,
    "--is-latest",
    "true",
  ]);
  assert.equal(ok.code, 0, ok.out);
  writeFileSync(
    lenovo,
    JSON.stringify({ ...host("main"), head: "b".repeat(40) })
  );
  const drift = cli(verifyReadback, [
    "--dell-receipt",
    dell,
    "--lenovo-receipt",
    lenovo,
    "--target-sha",
    target,
    "--is-latest",
    "true",
  ]);
  assert.notEqual(drift.code, 0);
  assert.match(drift.out, /mismatch|partial/);
});
