#!/usr/bin/env node
// Lenovo side: export the published base ref as one hashed git bundle.
// Runs as the single command of a signed job. Re-hashes the request file
// against the digest that travelled inside the MAC-bound job, observes the
// actual remote tip into an ISOLATED temp bare repo (the shared checkout is
// never written: no FETCH_HEAD, no ref updates, no worktree touch), writes
// <shared>/<bundle> plus its git-bundle-manifest/1 manifest, and prints the
// observed facts as one JSON line. Unreachable remote stops the export.
// Never pushes.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  fail,
  parseArgs,
  safeName,
  SHA_RE,
  BASE_REQUEST_SCHEMA,
  BASE_EXPORT_SCHEMA,
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
      schema: BASE_EXPORT_SCHEMA,
      outcome: "refused",
      reason,
      detail,
    })
  );
  fail(`base export refused: ${reason} ${detail}`);
  process.exit(1);
}

function git(checkout, ...args) {
  try {
    return execFileSync("git", ["-C", checkout, ...args], {
      encoding: "utf8",
    }).trim();
  } catch (error) {
    return null;
  }
}

const raw = (() => {
  try {
    return readFileSync(opts["--request"]);
  } catch (error) {
    refused("request-unreadable", error.message);
  }
})();
const actual = createHash("sha256").update(raw).digest("hex");
if (actual !== (opts["--request-sha256"] ?? "").toLowerCase()) {
  refused(
    "request-digest-mismatch",
    `${actual} != ${opts["--request-sha256"]}`
  );
}
let request;
try {
  request = JSON.parse(raw.toString("utf8"));
} catch (error) {
  refused("request-malformed", error.message);
}
if (request?.schema !== BASE_REQUEST_SCHEMA)
  refused("request-malformed", "bad schema");
for (const name of ["bundleName", "manifestName"]) {
  if (!safeName(request[name])) refused("request-malformed", `${name} unsafe`);
}
if (!String(request.refFull ?? "").startsWith("refs/heads/")) {
  refused("request-malformed", "refFull must be refs/heads/<branch>");
}
if (!existsSync(request.shared))
  refused("shared-unavailable", String(request.shared));
if (!existsSync(request.lenovoCheckout))
  refused("checkout-unavailable", String(request.lenovoCheckout));

// Freshness is proven here, not assumed: the actual remote tip is observed
// into an isolated temp bare repo. A stale Lenovo checkout cannot pass off
// its old ref as fresh, and the shared checkout is never written: no
// FETCH_HEAD, no ref updates, no worktree touch. Unreachable remote stops
// the export. Residual race (remote moves after this read) is caught by the
// mandatory preflight before push.
const originUrl = git(request.lenovoCheckout, "remote", "get-url", "origin");
if (originUrl === null || originUrl === "") {
  refused("remote-unavailable", "no origin remote");
}
const fetched = fetchRemoteTip({
  originUrl,
  refFull: request.refFull,
  token: request.token,
});
if (!fetched.ok) refused("remote-unavailable", fetched.detail);
const tip = fetched.sha;

const bundlePath = join(request.shared, request.bundleName);
if (existsSync(bundlePath))
  refused("artifact-exists", `refusing to replace ${request.bundleName}`);
try {
  // Bundle the observed remote ref itself (verified == remote SHA above),
  // so the bundle cannot drift from it.
  execFileSync("git", ["bundle", "create", bundlePath, fetched.ref], {
    cwd: fetched.repoDir,
  });
  const listed = execFileSync("git", ["bundle", "list-heads", bundlePath], {
    cwd: fetched.repoDir,
    encoding: "utf8",
  });
  const heads = listed.split("\n").map((l) => l.split(/\s+/)[0].toLowerCase());
  if (!heads.includes(tip))
    refused("bundle-tip-mismatch", heads.join(",").slice(0, 200));
} catch (error) {
  refused("git-failed", String(error.message).slice(0, 200));
} finally {
  removeDir(fetched.repoDir);
}

const data = readFileSync(bundlePath);
const sha256 = createHash("sha256").update(data).digest("hex");
const files = [{ path: request.bundleName, sha256, bytes: data.length }];
const manifestDigest = createHash("sha256")
  .update(Buffer.from(JSON.stringify(files), "utf8"))
  .digest("hex");
const manifest = {
  version: "git-bundle-manifest/1",
  revs: request.refFull,
  bundle: request.bundleName,
  files,
  manifestDigest,
};
mkdirSync(request.shared, { recursive: true });
writeFileSync(
  join(request.shared, request.manifestName),
  `${JSON.stringify(manifest)}\n`
);

let remotes = {};
const remoteNames = git(request.lenovoCheckout, "remote");
if (remoteNames !== null) {
  for (const name of remoteNames
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean)) {
    const url = git(request.lenovoCheckout, "remote", "get-url", name);
    if (url !== null) remotes[name] = url;
  }
}

console.log(
  JSON.stringify({
    schema: BASE_EXPORT_SCHEMA,
    token: request.token,
    refFull: request.refFull,
    tipSha: tip,
    bundleRef: fetched.ref,
    bundleName: request.bundleName,
    manifestName: request.manifestName,
    sha256,
    bytes: data.length,
    manifestDigest,
    remotes,
  })
);
