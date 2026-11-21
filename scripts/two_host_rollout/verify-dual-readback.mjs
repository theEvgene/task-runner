#!/usr/bin/env node
// Verify two dual-host receipts for issue #162. Reads the Dell dual receipt
// (apply-dual-host.mjs --receipt) and the Lenovo dual receipt (collect
// --out), checks the same two pins on both, and reports updated only when
// all four combos (2 hosts x 2 repos) verify. No git, no network.
import { readFileSync } from "node:fs";
import { DUAL_HOST_SCHEMA, combineDualHostResults } from "./dual-lib.mjs";
import { SHA_RE } from "./lib.mjs";

function usage(message) {
  process.stderr.write(`two-host-rollout verify-dual-readback: ${message}\n`);
  process.stderr.write(
    "usage: verify-dual-readback.mjs --dell-receipt <file> --lenovo-receipt <file> --task-runner-sha <sha> --pat-sha <sha> --is-latest true|false\n"
  );
  process.exit(2);
}

const opts = {};
for (let i = 2; i < process.argv.length; i += 2) {
  opts[process.argv[i]] = process.argv[i + 1];
}
for (const key of [
  "--dell-receipt",
  "--lenovo-receipt",
  "--task-runner-sha",
  "--pat-sha",
  "--is-latest",
]) {
  if (!opts[key]) usage(`${key} is required`);
}
const trt = String(opts["--task-runner-sha"]).toLowerCase();
const pt = String(opts["--pat-sha"]).toLowerCase();
if (!SHA_RE.test(trt)) usage("--task-runner-sha must be a full commit SHA");
if (!SHA_RE.test(pt)) usage("--pat-sha must be a full commit SHA");
if (!["true", "false"].includes(opts["--is-latest"]))
  usage("--is-latest must be true|false");
const isLatest = opts["--is-latest"] === "true";

function load(path, label) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "receipt-unreadable",
        detail: `${label}: ${error.message}`.slice(0, 200),
      })
    );
    process.exit(1);
  }
}

const hosts = [
  load(opts["--dell-receipt"], "dell"),
  load(opts["--lenovo-receipt"], "lenovo"),
];
for (const [index, host] of hosts.entries()) {
  const label = index === 0 ? "dell" : "lenovo";
  if (host?.schema !== DUAL_HOST_SCHEMA) {
    console.log(
      JSON.stringify({ verdict: "mismatch", reason: "receipt-malformed", detail: `${label}: bad schema` })
    );
    process.exit(1);
  }
}

const combined = combineDualHostResults(hosts, {
  taskRunnerTarget: trt,
  patTarget: pt,
  isLatest,
});
console.log(
  JSON.stringify({
    verdict: combined.verdict,
    reason: combined.reason,
    detail: combined.detail,
    taskRunnerSha: trt,
    patSha: pt,
    isLatest,
    nextAction: combined.nextAction,
  })
);
process.exit(combined.verdict === "updated" ? 0 : 1);
