#!/usr/bin/env node
// Verify two host receipts for issue #133. Reads Dell and Lenovo
// two-host-rollout-host/1 receipts, checks same target, branch/SHA/
// cleanliness rules, and managed skill state. No git, no network.
import { readFileSync } from "node:fs";
import { HOST_SCHEMA, SHA_RE } from "./lib.mjs";

function usage(message) {
  process.stderr.write(`two-host-rollout verify-readback: ${message}\n`);
  process.stderr.write(
    "usage: verify-readback.mjs --dell-receipt <file> --lenovo-receipt <file> --target-sha <sha> --is-latest true|false\n"
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
  "--target-sha",
  "--is-latest",
]) {
  if (!opts[key]) usage(`${key} is required`);
}
const target = String(opts["--target-sha"]).toLowerCase();
if (!SHA_RE.test(target)) usage("--target-sha must be a full commit SHA");
if (!["true", "false"].includes(opts["--is-latest"]))
  usage("--is-latest must be true|false");
const isLatest = opts["--is-latest"] === "true";

function load(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "receipt-unreadable",
        detail: `${path}: ${error.message}`.slice(0, 200),
      })
    );
    process.exit(1);
  }
}

const hosts = [load(opts["--dell-receipt"]), load(opts["--lenovo-receipt"])];
for (const [index, host] of hosts.entries()) {
  const label = index === 0 ? "dell" : "lenovo";
  if (host?.schema !== HOST_SCHEMA) {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "receipt-malformed",
        detail: `${label}: bad schema`,
      })
    );
    process.exit(1);
  }
  if (!["updated", "already-current"].includes(host.verdict)) {
    console.log(
      JSON.stringify({
        verdict: "partial",
        reason: host.reason ?? "host-failed",
        detail: `${label}: ${host.verdict}`,
      })
    );
    process.exit(1);
  }
  if (
    String(host.targetSha ?? "").toLowerCase() !== target ||
    Boolean(host.isLatest) !== isLatest
  ) {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "revision-mismatch",
        detail: `${label}: target mismatch`,
      })
    );
    process.exit(1);
  }
  if (String(host.head ?? "").toLowerCase() !== target || host.clean !== true) {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "revision-mismatch",
        detail: `${label}: head/clean mismatch`,
      })
    );
    process.exit(1);
  }
  if (isLatest && host.branch !== "main") {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "wrong-branch",
        detail: `${label}: expected main`,
      })
    );
    process.exit(1);
  }
  if (isLatest && String(host.originMain ?? "").toLowerCase() !== target) {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "revision-mismatch",
        detail: `${label}: originMain != target`,
      })
    );
    process.exit(1);
  }
  if (host.skills?.state !== "current") {
    console.log(
      JSON.stringify({
        verdict: "mismatch",
        reason: "skills-not-current",
        detail: `${label}: skills ${host.skills?.state}`,
      })
    );
    process.exit(1);
  }
}
console.log(
  JSON.stringify({
    verdict: "updated",
    reason: "updated",
    targetSha: target,
    isLatest,
  })
);
