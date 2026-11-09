#!/usr/bin/env node
// Lenovo side: read-only probe of the target branch tip. Re-hashes the
// request against the digest inside the MAC-bound job, observes the actual
// remote tip into an isolated temp bare repo, then reads it. Writes nothing
// anywhere: the shared checkout keeps no FETCH_HEAD, no ref updates, no
// worktree touch. Unreachable remote refuses.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import {
  fail,
  parseArgs,
  SHA_RE,
  TIP_REQUEST_SCHEMA,
  TIP_READ_SCHEMA,
  fetchRemoteTip,
  removeDir,
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
let fetched = null;
try {
  const originUrl = execFileSync(
    "git",
    ["-C", request.lenovoCheckout, "remote", "get-url", "origin"],
    { encoding: "utf8" }
  ).trim();
  if (!originUrl) refused("remote-unavailable", "no origin remote");
  fetched = fetchRemoteTip({
    originUrl,
    refFull: request.refFull,
    token: request.token,
  });
  if (!fetched.ok) refused("remote-unavailable", fetched.detail);
  tip = fetched.sha;
} catch (error) {
  refused("remote-unavailable", String(error.message).slice(0, 200));
} finally {
  if (fetched?.repoDir) removeDir(fetched.repoDir);
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
