import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const collectScript = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "collect-lenovo-result.mjs"
);
const verifyReadback = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "verify-readback.mjs"
);
const BRIDGE = join(repoRoot, "scripts", "lan_bridge", "lan_bridge.py");
const PYTHON =
  process.env.TWO_HOST_ROLLOUT_PYTHON ??
  "C:\\Users\\rb199787\\AppData\\Local\\Programs\\Python\\Python313\\python.exe";

const SEAL_RESULT = `
import sys
bridge_dir, queue, key_file, key_format, job_id, status, exit_code, stdout_file = sys.argv[1:9]
sys.path.insert(0, bridge_dir)
import lan_bridge
key = lan_bridge.load_key(key_file, key_format, queue)
with open(stdout_file, encoding="utf-8") as fh:
    stdout = fh.read()
payload = lan_bridge.build_result_payload(job_id, status, int(exit_code), stdout, "")
lan_bridge.publish(queue + "/results", job_id, lan_bridge.seal(payload, key))
`;

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

function py(args) {
  return execFileSync(PYTHON, args, { encoding: "utf8" });
}

// Production path through the real signed client: submit seals the rollout
// job (MAC), the fixture seals the Lenovo stdout as its result (MAC, bound
// id), collect fetches the verified envelope and saves only the checked
// receipt. No live host, no network.
function makeWorld() {
  const root = mkdtempSync(join(tmpdir(), "two-host-collect-"));
  const queue = join(root, "queue");
  mkdirSync(join(queue, "jobs"), { recursive: true });
  mkdirSync(join(queue, "results"), { recursive: true });
  const keyFile = join(root, "keys", "queue.key");
  mkdirSync(join(root, "keys"), { recursive: true });
  writeFileSync(keyFile, randomBytes(32));
  return { root, queue, keyFile };
}

function submitRolloutJob({ queue, keyFile, target }) {
  const cmdFile = join(queue, "..", `cmd-${target.slice(0, 8)}.txt`);
  writeFileSync(
    cmdFile,
    `node C:/lenovo/task-runner/scripts/two_host_rollout/apply-host.mjs --checkout C:/lenovo/task-runner --target-sha ${target} --is-latest true\n`
  );
  const out = py([
    BRIDGE,
    "submit",
    "--queue",
    queue,
    "--key-file",
    keyFile,
    "--command-file",
    cmdFile,
    "--cwd",
    queue,
    "--timeout",
    "300",
    "--max-output",
    "65536",
  ]);
  const m = out.match(/JOBID=([0-9a-f]{32})/);
  assert.ok(m, `submit printed no JOBID: ${out}`);
  return m[1];
}

function sealStdout({ queue, keyFile, jobId, status, exitCode, stdout }) {
  const outFile = join(queue, "..", `stdout-${jobId}.json`);
  writeFileSync(outFile, stdout);
  py([
    "-c",
    SEAL_RESULT,
    dirname(BRIDGE),
    queue,
    keyFile,
    "raw",
    jobId,
    status,
    String(exitCode),
    outFile,
  ]);
}

function hostReceipt(over = {}) {
  const t = "a".repeat(40);
  return {
    schema: "two-host-rollout-host/1",
    verdict: "updated",
    reason: "updated",
    targetSha: t,
    isLatest: true,
    branch: "main",
    head: t,
    clean: true,
    skills: { state: "current" },
    ...over,
  };
}

function collect({ queue, keyFile, jobId, target, isLatest, out }) {
  return cli(collectScript, [
    "--queue",
    queue,
    "--key-file",
    keyFile,
    "--job-id",
    jobId,
    "--target-sha",
    target,
    "--is-latest",
    isLatest,
    "--out",
    out,
    "--bridge",
    BRIDGE,
    "--python",
    PYTHON,
    "--fetch-timeout",
    "5",
    "--poll",
    "0.2",
  ]);
}

test("collect saves the MAC-verified lenovo receipt; pair combines to updated", () => {
  const target = "a".repeat(40);
  const w = makeWorld();
  const jobId = submitRolloutJob({ ...w, target });
  sealStdout({
    ...w,
    jobId,
    status: "ok",
    exitCode: 0,
    stdout: JSON.stringify(hostReceipt()),
  });
  const out = join(w.root, "lenovo.receipt.json");
  const r = collect({ ...w, jobId, target, isLatest: "true", out });
  assert.equal(r.code, 0, r.out);
  assert.ok(existsSync(out));
  const saved = JSON.parse(readFileSync(out, "utf8"));
  assert.equal(saved.head, target);
  const dell = join(w.root, "dell.receipt.json");
  writeFileSync(dell, JSON.stringify(hostReceipt()));
  const v = cli(verifyReadback, [
    "--dell-receipt",
    dell,
    "--lenovo-receipt",
    out,
    "--target-sha",
    target,
    "--is-latest",
    "true",
  ]);
  assert.equal(v.code, 0, v.out);
  assert.match(v.out, /"updated"/);
});

test("partial failure: refused lenovo receipt is saved and combines to partial", () => {
  const target = "a".repeat(40);
  const w = makeWorld();
  const jobId = submitRolloutJob({ ...w, target });
  sealStdout({
    ...w,
    jobId,
    status: "error",
    exitCode: 1,
    stdout: JSON.stringify(
      hostReceipt({ verdict: "refused", reason: "dirty-checkout" })
    ),
  });
  const out = join(w.root, "lenovo.receipt.json");
  const r = collect({ ...w, jobId, target, isLatest: "true", out });
  assert.equal(r.code, 0, r.out);
  const dell = join(w.root, "dell.receipt.json");
  writeFileSync(dell, JSON.stringify(hostReceipt()));
  const v = cli(verifyReadback, [
    "--dell-receipt",
    dell,
    "--lenovo-receipt",
    out,
    "--target-sha",
    target,
    "--is-latest",
    "true",
  ]);
  assert.notEqual(v.code, 0);
  assert.match(v.out, /partial/);
  assert.match(v.out, /dirty-checkout/);
});

test("conflict: ownership-conflict receipt is saved, nothing trusted blindly", () => {
  const target = "a".repeat(40);
  const w = makeWorld();
  const jobId = submitRolloutJob({ ...w, target });
  const refused = hostReceipt({
    verdict: "refused",
    reason: "ownership-conflict",
    detail: "foreign skill dir",
  });
  sealStdout({
    ...w,
    jobId,
    status: "error",
    exitCode: 1,
    stdout: JSON.stringify(refused),
  });
  const out = join(w.root, "lenovo.receipt.json");
  const r = collect({ ...w, jobId, target, isLatest: "true", out });
  assert.equal(r.code, 0, r.out);
  assert.equal(JSON.parse(readFileSync(out, "utf8")).reason, "ownership-conflict");
  const dell = join(w.root, "dell.receipt.json");
  writeFileSync(dell, JSON.stringify(hostReceipt()));
  const v = cli(verifyReadback, [
    "--dell-receipt",
    dell,
    "--lenovo-receipt",
    out,
    "--target-sha",
    target,
    "--is-latest",
    "true",
  ]);
  assert.notEqual(v.code, 0);
  assert.match(v.out, /partial/);
  assert.match(v.out, /ownership-conflict/);
});

test("repeated run: already-current collects idempotently to updated", () => {
  const target = "a".repeat(40);
  const w = makeWorld();
  const jobId = submitRolloutJob({ ...w, target });
  sealStdout({
    ...w,
    jobId,
    status: "ok",
    exitCode: 0,
    stdout: JSON.stringify(hostReceipt({ verdict: "already-current" })),
  });
  const out = join(w.root, "lenovo.receipt.json");
  const first = collect({ ...w, jobId, target, isLatest: "true", out });
  assert.equal(first.code, 0, first.out);
  const bytes = readFileSync(out);
  const second = collect({ ...w, jobId, target, isLatest: "true", out });
  assert.equal(second.code, 0, second.out);
  assert.equal(readFileSync(out).toString(), bytes.toString());
  const dell = join(w.root, "dell.receipt.json");
  writeFileSync(dell, JSON.stringify(hostReceipt({ verdict: "already-current" })));
  const v = cli(verifyReadback, [
    "--dell-receipt",
    dell,
    "--lenovo-receipt",
    out,
    "--target-sha",
    target,
    "--is-latest",
    "true",
  ]);
  assert.equal(v.code, 0, v.out);
});

test("unknown keeps the slot: short-timeout expiry writes nothing, same-id resume", () => {
  const target = "a".repeat(40);
  const w = makeWorld();
  // The job is sealed but the worker has not finished: no result file yet.
  // The bounded wait expires as unknown; the slot stays occupied for the
  // SAME id, never a new submit.
  const jobId = submitRolloutJob({ ...w, target });
  const out = join(w.root, "lenovo.receipt.json");
  const r = cli(collectScript, [
    "--queue",
    w.queue,
    "--key-file",
    w.keyFile,
    "--job-id",
    jobId,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--out",
    out,
    "--bridge",
    BRIDGE,
    "--python",
    PYTHON,
    "--fetch-timeout",
    "1",
    "--poll",
    "0.2",
  ]);
  assert.equal(r.code, 3, r.out);
  assert.match(r.out, /SAME job id/);
  assert.equal(existsSync(out), false);
});
