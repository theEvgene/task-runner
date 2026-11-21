#!/usr/bin/env node
// Dell side: save the authenticated dual Lenovo host receipt for issue #162.
//
// The Lenovo dual apply receipt arrives ONLY as stdout of the MAC-verified
// same-job fetch through the existing signed client (lan_bridge.py fetch:
// MAC + kind/id binding verified there), never by hand-copying a file.
// Exit 0 collected (a refused dual verdict is still a saved verified
// terminal outcome and combines to partial, never to updated).
// Exit 1 terminal mismatch/failure (NEW id only after fixing inputs; never
// an automatic retry). Exit 3 outcome unknown: the bounded wait expired,
// the slot stays occupied, re-fetch the SAME job id, never a new one.
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { JOB_RE, SHA_RE } from "./lib.mjs";
import { verifyDualLenovoPayload } from "./dual-lib.mjs";
import {
  assertKeyOutside,
  fetchResult,
  readJob,
  resolveBridge,
  resolvePython,
} from "./bridge.mjs";

const SCHEMA = "two-host-dual-collect/1";

const opts = {};
const repeatable = new Set(["--protect"]);
for (let i = 2; i < process.argv.length; i += 1) {
  const key = process.argv[i];
  if (
    ![
      "--queue",
      "--key-file",
      "--key-format",
      "--job-id",
      "--task-runner-sha",
      "--pat-sha",
      "--is-latest",
      "--out",
      "--bridge",
      "--python",
      "--fetch-timeout",
      "--poll",
      "--protect",
    ].includes(key)
  ) {
    usage(`unknown argument: ${key}`);
  }
  const value = process.argv[i + 1];
  if (value === undefined || (!repeatable.has(key) && value.startsWith("--")))
    usage(`${key} requires a value`);
  if (repeatable.has(key)) {
    opts[key] = opts[key] ?? [];
    opts[key].push(value);
  } else {
    opts[key] = value;
  }
  i += 1;
}

function usage(message) {
  process.stderr.write(`two-host-rollout collect-dual-result: ${message}\n`);
  process.stderr.write(
    "usage: collect-dual-result.mjs --queue <dir> --key-file <file> [--key-format raw|base64] --job-id <32hex> --task-runner-sha <sha> --pat-sha <sha> --is-latest true|false --out <file> --bridge <lan_bridge.py> [--python <exe>] [--fetch-timeout <s>] [--poll <s>] [--protect <dir>...]\n"
  );
  process.exit(2);
}

for (const key of [
  "--queue",
  "--key-file",
  "--job-id",
  "--task-runner-sha",
  "--pat-sha",
  "--is-latest",
  "--out",
]) {
  if (!opts[key]) usage(`${key} is required`);
}
const keyFormat = opts["--key-format"] ?? "raw";
if (!["raw", "base64"].includes(keyFormat)) usage("--key-format must be raw|base64");
if (!JOB_RE.test(opts["--job-id"])) usage("--job-id must be 32 lowercase hex");
const trt = String(opts["--task-runner-sha"]).toLowerCase();
const pt = String(opts["--pat-sha"]).toLowerCase();
if (!SHA_RE.test(trt)) usage("--task-runner-sha must be a full commit SHA");
if (!SHA_RE.test(pt)) usage("--pat-sha must be a full commit SHA");
if (!["true", "false"].includes(opts["--is-latest"]))
  usage("--is-latest must be true|false");
const isLatest = opts["--is-latest"] === "true";
const outPath = resolve(opts["--out"]);
for (const [label, value] of [
  ["--queue", opts["--queue"]],
  ["--out", outPath],
]) {
  if (!isAbsolute(value)) usage(`${label} must be absolute`);
}
const bridge = resolveBridge(opts);
const python = resolvePython(opts);
assertKeyOutside(resolve(opts["--key-file"]), [
  resolve(opts["--queue"]),
  ...(opts["--protect"] ?? []),
]);

const fetchTimeout = Number(opts["--fetch-timeout"] ?? "60");
const poll = Number(opts["--poll"] ?? "1");
if (!Number.isFinite(fetchTimeout) || fetchTimeout <= 0)
  usage("--fetch-timeout must be a positive number of seconds");
if (!Number.isFinite(poll) || poll <= 0) usage("--poll must be positive");

function report(exit, body) {
  console.log(JSON.stringify(body));
  process.exit(exit);
}

// The sealed job must carry exactly this dual rollout's Lenovo command:
// refuse a result bound to any other job before trusting its bytes.
let job;
try {
  job = readJob({
    python,
    bridge,
    queue: opts["--queue"],
    keyFile: opts["--key-file"],
    keyFormat,
    id: opts["--job-id"],
  });
} catch (error) {
  report(1, {
    schema: SCHEMA,
    verdict: "mismatch",
    reason: "queue-job-unverifiable",
    jobId: opts["--job-id"],
    detail: String(error.message).slice(0, 400),
    nextAction: "stop and reconcile before any retry",
  });
}
const command = String(job.command ?? "");
if (
  !command.includes("apply-dual-host.mjs") ||
  !command.toLowerCase().includes(trt) ||
  !command.toLowerCase().includes(pt)
) {
  report(1, {
    schema: SCHEMA,
    verdict: "mismatch",
    reason: "queue-job-mismatch",
    jobId: opts["--job-id"],
    detail: "sealed job is not this dual rollout's apply-dual-host command",
    nextAction: "fetch the job id built for these two pins, never another id",
  });
}

// Bounded wait through the standard client; a missing slot is unknown.
// Timeout keeps the slot: re-fetch the SAME id, never a new one.
const fetched = fetchResult({
  python,
  bridge,
  queue: opts["--queue"],
  keyFile: opts["--key-file"],
  keyFormat,
  id: opts["--job-id"],
  timeout: fetchTimeout,
  poll,
});
if (fetched.code === 3) {
  report(3, {
    schema: SCHEMA,
    verdict: "unknown",
    reason: "outcome-unknown",
    jobId: opts["--job-id"],
    detail: `bounded wait expired; slot stays occupied (${fetchTimeout}s)`,
    nextAction: "re-fetch the SAME job id; never submit a new id while unknown",
  });
}
if (fetched.code !== 0) {
  report(1, {
    schema: SCHEMA,
    verdict: "mismatch",
    reason: "queue-result-unverifiable",
    jobId: opts["--job-id"],
    detail: String(fetched.stderr).slice(0, 400),
    nextAction: "stop and reconcile before any retry",
  });
}

const checked = verifyDualLenovoPayload(fetched.payload, {
  taskRunnerTarget: trt,
  patTarget: pt,
  isLatest,
});
if (!checked.ok) {
  const terminalNewId = ["worker-timeout", "output-truncated"].includes(checked.reason);
  report(1, {
    schema: SCHEMA,
    verdict: "mismatch",
    reason: checked.reason,
    jobId: opts["--job-id"],
    detail: checked.detail,
    nextAction: terminalNewId
      ? "fix worker limits/output size, then a NEW job id"
      : "stop and reconcile before any retry",
  });
}

mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, `${JSON.stringify(checked.receipt, null, 2)}\n`);
report(0, {
  schema: SCHEMA,
  verdict: "collected",
  jobId: opts["--job-id"],
  hostVerdict: checked.receipt.verdict,
  hostReason: checked.receipt.reason,
  out: outPath,
  nextAction:
    checked.receipt.verdict === "refused"
      ? "verified terminal refusal: combine for partial, fix per nextAction, rerun the same two revisions"
      : "combine --out with the Dell dual receipt in verify-dual-readback.mjs",
});
