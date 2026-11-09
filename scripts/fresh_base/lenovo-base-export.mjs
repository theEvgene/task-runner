#!/usr/bin/env node
// Lenovo side: export the published base ref as one hashed git bundle.
// Runs as the single command of a signed job. Re-hashes the request file
// against the digest that travelled inside the MAC-bound job, writes
// <shared>/<bundle> plus its git-bundle-manifest/1 manifest, and prints the
// observed facts as one JSON line. Never pushes, never edits the checkout.
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
    refused(
      "git-failed",
      `git ${args.join(" ")}: ${String(error.stderr ?? error.message).slice(0, 200)}`
    );
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

const tip = git(
  request.lenovoCheckout,
  "rev-parse",
  "--verify",
  `${request.refFull}^{commit}`
).toLowerCase();
if (!SHA_RE.test(tip))
  refused("git-failed", "rev-parse did not return a commit");

const bundlePath = join(request.shared, request.bundleName);
if (existsSync(bundlePath))
  refused("artifact-exists", `refusing to replace ${request.bundleName}`);
git(request.lenovoCheckout, "bundle", "create", bundlePath, request.refFull);
const listed = git(request.lenovoCheckout, "bundle", "list-heads", bundlePath);
const heads = listed.split("\n").map((l) => l.split(/\s+/)[0].toLowerCase());
if (!heads.includes(tip))
  refused("bundle-tip-mismatch", heads.join(",").slice(0, 200));

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
try {
  for (const name of git(request.lenovoCheckout, "remote")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean)) {
    remotes[name] = git(request.lenovoCheckout, "remote", "get-url", name);
  }
} catch {
  remotes = {};
}

console.log(
  JSON.stringify({
    schema: BASE_EXPORT_SCHEMA,
    token: request.token,
    refFull: request.refFull,
    tipSha: tip,
    bundleName: request.bundleName,
    manifestName: request.manifestName,
    sha256,
    bytes: data.length,
    manifestDigest,
    remotes,
  })
);
