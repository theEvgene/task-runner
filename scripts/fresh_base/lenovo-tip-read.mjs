#!/usr/bin/env node
// Lenovo side: read-only probe of the target branch tip. Re-hashes the
// request against the digest inside the MAC-bound job, reads the ref with
// rev-parse, prints one JSON line, writes nothing.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import {
  fail,
  parseArgs,
  SHA_RE,
  TIP_REQUEST_SCHEMA,
  TIP_READ_SCHEMA,
} from "./lib.mjs";

const opts = parseArgs(process.argv.slice(2), {
  "--request": "required",
  "--request-sha256": "required",
});

function refused(reason, detail = "") {
  console.log(
    JSON.stringify({
      schema: TIP_READ_SCHEMA,
      outcome: "refused",
      reason,
      detail,
    })
  );
  fail(`tip read refused: ${reason} ${detail}`);
  process.exit(1);
}

let raw;
try {
  raw = readFileSync(opts["--request"]);
} catch (error) {
  refused("request-unreadable", error.message);
}
if (
  createHash("sha256").update(raw).digest("hex") !==
  (opts["--request-sha256"] ?? "").toLowerCase()
) {
  refused("request-digest-mismatch", "digest differs");
}
let request;
try {
  request = JSON.parse(raw.toString("utf8"));
} catch (error) {
  refused("request-malformed", error.message);
}
if (request?.schema !== TIP_REQUEST_SCHEMA)
  refused("request-malformed", "bad schema");
if (!String(request.refFull ?? "").startsWith("refs/heads/"))
  refused("request-malformed", "bad refFull");
if (!existsSync(request.lenovoCheckout))
  refused("checkout-unavailable", String(request.lenovoCheckout));

let tip;
try {
  tip = execFileSync(
    "git",
    [
      "-C",
      request.lenovoCheckout,
      "rev-parse",
      "--verify",
      `${request.refFull}^{commit}`,
    ],
    {
      encoding: "utf8",
    }
  )
    .trim()
    .toLowerCase();
} catch (error) {
  refused("git-failed", String(error.message).slice(0, 200));
}
if (!SHA_RE.test(tip))
  refused("git-failed", "rev-parse did not return a commit");

console.log(
  JSON.stringify({
    schema: TIP_READ_SCHEMA,
    token: request.token,
    refFull: request.refFull,
    tipSha: tip,
  })
);
