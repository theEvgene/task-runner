#!/usr/bin/env node
// Dell side: late movement gate. Fetches the sealed tip-probe result for one
// Lenovo job through the existing standard client (lan_bridge.py fetch: MAC
// + kind/id binding verified there) and compares it with the early verified
// SHA in the import receipt. Run again immediately before push: the early
// receipt is never a push-time guarantee.
//
// Exit 0 fresh (tips match); 2 moved/stale; 1 verification/mismatch failure;
// 3 unknown (bounded wait timed out; slot stays occupied, re-fetch the SAME
// job id, never a new one). Read-only: changes nothing.
import { readFileSync } from "node:fs";
import {
  JOB_RE,
  SHA_RE,
  fail,
  parseArgs,
  shortSha,
  TIP_READ_SCHEMA,
} from "./lib.mjs";
import {
  fetchResult,
  assertKeyOutside,
  readJob,
  resolveBridge,
  resolvePython,
} from "./bridge.mjs";

const opts = parseArgs(process.argv.slice(2), {
  "--queue": "required",
  "--key-file": "required",
  "--key-format": "optional",
  "--job-id": "required",
  "--receipt": "required",
  "--fetch-timeout": "optional",
  "--poll": "optional",
  "--bridge": "optional",
  "--python": "optional",
  "--protect": "repeatable",
});

if (!JOB_RE.test(opts["--job-id"] ?? "")) {
  fail("--job-id must be 32 lowercase hex");
  process.exit(2);
}

let receipt;
try {
  receipt = JSON.parse(readFileSync(opts["--receipt"], "utf8"));
} catch (error) {
  fail(`cannot read receipt: ${error.message}`);
  process.exit(1);
}
if (!SHA_RE.test(receipt?.baseSha ?? "")) {
  fail("receipt has no verified base SHA");
  process.exit(1);
}
if (!receipt?.refFull) {
  fail("receipt has no refFull binding");
  process.exit(1);
}

const keyFormat = opts["--key-format"] ?? "raw";
const bridge = resolveBridge(opts);
const python = resolvePython(opts);
assertKeyOutside(opts["--key-file"], [
  opts["--queue"],
  ...(opts["--protect"] ?? []),
]);

try {
  const job = readJob({
    python,
    bridge,
    queue: opts["--queue"],
    keyFile: opts["--key-file"],
    keyFormat,
    id: opts["--job-id"],
  });
  if (!String(job.command ?? "").includes("lenovo-tip-read.mjs")) {
    fail(
      `probe job carries an unexpected command: ${String(job.command ?? "").slice(0, 160)}`
    );
    process.exit(1);
  }
} catch (error) {
  fail(`probe job unverifiable: ${error.message}`);
  process.exit(1);
}

const fetched = fetchResult({
  python,
  bridge,
  queue: opts["--queue"],
  keyFile: opts["--key-file"],
  keyFormat,
  id: opts["--job-id"],
  timeout: Number(opts["--fetch-timeout"] ?? "60"),
  poll: Number(opts["--poll"] ?? "1"),
});
if (fetched.code === 3) {
  fail("probe outcome unknown: slot stays occupied, re-fetch the SAME job id");
  process.exit(3);
}
if (fetched.code !== 0) {
  fail(`probe result unverifiable: ${fetched.stderr.slice(0, 400)}`);
  process.exit(1);
}
const payload = fetched.payload;
if (
  payload.status !== "ok" ||
  payload.exit_code !== 0 ||
  payload.timed_out ||
  payload.output_truncated
) {
  fail(`probe job failed: ${String(payload.stderr ?? "").slice(0, 200)}`);
  process.exit(1);
}
let observed;
try {
  observed = JSON.parse(String(payload.stdout ?? ""));
} catch {
  fail("probe payload is not JSON");
  process.exit(1);
}
if (
  observed?.schema !== TIP_READ_SCHEMA ||
  !SHA_RE.test(observed?.tipSha ?? "")
) {
  fail("probe payload is not a factual tip read");
  process.exit(1);
}
if (observed.refFull !== receipt.refFull) {
  fail(`probe ref ${observed.refFull} != receipt ref ${receipt.refFull}`);
  process.exit(1);
}

const result = {
  verifiedSha: receipt.baseSha,
  currentSha: String(observed.tipSha).toLowerCase(),
  fresh:
    String(observed.tipSha).toLowerCase() ===
    String(receipt.baseSha).toLowerCase(),
};
console.log(JSON.stringify(result));
if (!result.fresh) {
  fail(
    `branch ${receipt.refFull} moved after early check (${shortSha(result.verifiedSha)} -> ${shortSha(result.currentSha)}); rebase required`
  );
  process.exit(2);
}
