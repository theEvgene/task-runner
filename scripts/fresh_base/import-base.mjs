#!/usr/bin/env node
// Dell side: receive, verify, and import the Lenovo base copy exactly once.
//
// Trust arrives only inside the MAC-sealed result bound to --job-id, consumed
// through the existing standard client (lan_bridge.py fetch: MAC + kind/id
// binding verified there). Bundle SHA-256, byte count, and tip commit are
// re-checked against it, so bytes swapped on the share cannot be imported as
// this job's base. The copy is created once at --repo-out and never replaced.
// Dell never contacts the corporate remote; only Lenovo reads it.
//
// The submit step is the standard client itself (see docs/fresh-base.md).
// Exit 0 imported; 1 refused/mismatch; 2 usage; 3 unknown (slot stays
// occupied: re-fetch the SAME job id, never a new one).
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import {
  BRANCH_RE,
  JOB_RE,
  SHA_RE,
  SHA256_RE,
  fail,
  parseArgs,
  safeName,
  BASE_EXPORT_SCHEMA,
} from "./lib.mjs";
import {
  assertKeyOutside,
  fetchResult,
  readJob,
  resolveBridge,
  resolvePython,
} from "./bridge.mjs";

const SCHEMA = "fresh-base-import/1";

const opts = parseArgs(process.argv.slice(2), {
  "--queue": "required",
  "--key-file": "required",
  "--key-format": "optional",
  "--job-id": "required",
  "--shared": "required",
  "--repo": "required",
  "--repo-out": "required",
  "--branch": "optional",
  "--expected-project": "optional",
  "--receipt": "required",
  "--fetch-timeout": "optional",
  "--poll": "optional",
  "--provenance": "optional",
  "--bridge": "optional",
  "--python": "optional",
});

function usage(message) {
  fail(`usage: ${message}`);
  process.exit(2);
}

if (!JOB_RE.test(opts["--job-id"] ?? ""))
  usage("--job-id must be 32 lowercase hex");
for (const n of ["queue", "repo", "shared"]) {
  if (!isAbsolute(opts[`--${n}`] ?? ""))
    usage(`--${n} must be absolute: the key is proven outside it`);
}
const repoOut = resolve(opts["--repo-out"]);
if (
  resolve(opts["--receipt"])
    .toLowerCase()
    .startsWith(`${repoOut.toLowerCase()}\\`)
) {
  usage("--receipt must stay outside the imported copy");
}
const branch = opts["--branch"] ?? "main";
if (!BRANCH_RE.test(branch)) usage("--branch is not a safe name");
const provenance = opts["--provenance"] ?? "fixture";
if (!["fixture", "live"].includes(provenance))
  usage("--provenance must be fixture|live");

const keyFormat = opts["--key-format"] ?? "raw";
const bridge = resolveBridge(opts);
const python = resolvePython(opts);
assertKeyOutside(opts["--key-file"], [
  opts["--queue"],
  opts["--repo"],
  repoOut,
  opts["--shared"],
]);

const receipt = {
  schema: SCHEMA,
  provenance,
  verdict: null,
  reason: null,
  detail: "",
  jobId: opts["--job-id"],
  baseSha: null,
  refFull: null,
  bundle: { name: null, sha256: null, bytes: null, verified: false },
  manifest: { digest: null, match: false },
  repo: { path: repoOut, created: false, head: null },
  nextAction: null,
};

function finish(code, verdict, reason, detail = "") {
  receipt.verdict = verdict;
  receipt.reason = reason;
  receipt.detail = detail.slice(0, 400);
  receipt.nextAction =
    {
      imported:
        "cut the task worktree from the imported base and start local work",
      "repo-exists": "choose a new empty path; one source copy, never replaced",
      "outcome-unknown":
        "keep the slot, re-fetch the SAME job id; no second import",
    }[reason] ?? "stop and reconcile before any retry";
  mkdirSync(resolve(opts["--receipt"], ".."), { recursive: true });
  writeFileSync(opts["--receipt"], `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(JSON.stringify(receipt));
  process.exit(code);
}

function projectOf(url) {
  if (!url) return null;
  const c = String(url).trim();
  if (c.startsWith("git@")) {
    const p = c.slice(4).split(":")[1] ?? "";
    return p.includes("/") ? p.replace(/\.git$/, "") : null;
  }
  if (!c.includes("://")) return null; // a local path names no project
  try {
    const u = new URL(c);
    const p = u.pathname.replace(/^\//, "");
    return p.includes("/") ? p.replace(/\.git$/, "") : null;
  } catch {
    return null;
  }
}

// The sealed job must carry exactly the documented export command.
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
  finish(1, "rejected", "queue-job-unverifiable", error.message);
}
if (!String(job.command ?? "").includes("lenovo-base-export.mjs")) {
  finish(
    1,
    "rejected",
    "queue-job-mismatch",
    String(job.command ?? "").slice(0, 200)
  );
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
  timeout: Number(opts["--fetch-timeout"] ?? "60"),
  poll: Number(opts["--poll"] ?? "1"),
});
if (fetched.code === 3)
  finish(3, "unknown", "outcome-unknown", opts["--job-id"]);
if (fetched.code !== 0)
  finish(
    1,
    "rejected",
    "queue-result-unverifiable",
    fetched.stderr.slice(0, 400)
  );
const payload = fetched.payload;

const failed =
  payload.status !== "ok" ||
  payload.exit_code !== 0 ||
  payload.timed_out ||
  payload.output_truncated;
if (failed) {
  finish(
    1,
    "rejected",
    "base-job-failed",
    String(payload.stderr ?? "").slice(0, 400)
  );
}

let exported;
try {
  exported = JSON.parse(String(payload.stdout ?? ""));
} catch (error) {
  finish(1, "mismatch", "request-malformed", error.message);
}
if (exported?.schema !== BASE_EXPORT_SCHEMA || exported.outcome === "refused") {
  finish(
    1,
    "mismatch",
    "request-malformed",
    String(exported?.reason ?? "bad schema").slice(0, 200)
  );
}
const tip = String(exported.tipSha ?? "").toLowerCase();
if (!SHA_RE.test(tip) || !SHA256_RE.test(String(exported.sha256 ?? ""))) {
  finish(1, "mismatch", "request-malformed", "tipSha/sha256 are not factual");
}
for (const n of ["bundleName", "manifestName"]) {
  if (!safeName(exported[n]))
    finish(1, "mismatch", "request-malformed", `${n} unsafe`);
}
if (!String(exported.refFull ?? "").startsWith("refs/heads/")) {
  finish(
    1,
    "mismatch",
    "request-malformed",
    "refFull must be refs/heads/<branch>"
  );
}
receipt.baseSha = tip;
receipt.refFull = exported.refFull;
receipt.bundle = {
  name: exported.bundleName,
  sha256: String(exported.sha256).toLowerCase(),
  bytes: exported.bytes,
  verified: false,
};

const bundlePath = join(resolve(opts["--shared"]), exported.bundleName);
if (!existsSync(bundlePath))
  finish(1, "mismatch", "artifact-missing", exported.bundleName);
const bytes = readFileSync(bundlePath);
if (bytes.length !== exported.bytes)
  finish(1, "mismatch", "artifact-mismatch", "byte count differs");
if (
  createHash("sha256").update(bytes).digest("hex") !== receipt.bundle.sha256
) {
  finish(
    1,
    "mismatch",
    "artifact-mismatch",
    "bundle SHA-256 differs from the sealed result"
  );
}
receipt.bundle.verified = true;

let manifest;
try {
  manifest = JSON.parse(
    readFileSync(join(resolve(opts["--shared"]), exported.manifestName), "utf8")
  );
} catch (error) {
  finish(1, "mismatch", "manifest-mismatch", error.message);
}
const okManifest =
  manifest?.version === "git-bundle-manifest/1" &&
  Array.isArray(manifest.files) &&
  manifest.files.length === 1 &&
  manifest.files[0]?.path === exported.bundleName &&
  String(manifest.files[0]?.sha256 ?? "").toLowerCase() ===
    receipt.bundle.sha256 &&
  manifest.files[0]?.bytes === exported.bytes &&
  createHash("sha256")
    .update(Buffer.from(JSON.stringify(manifest.files), "utf8"))
    .digest("hex") === manifest.manifestDigest &&
  manifest.manifestDigest === exported.manifestDigest;
if (!okManifest)
  finish(
    1,
    "mismatch",
    "manifest-mismatch",
    "manifest does not cover the sealed bundle"
  );
receipt.manifest = { digest: manifest.manifestDigest, match: true };

if (opts["--expected-project"]) {
  const projects = Object.values(exported.remotes ?? {})
    .map(projectOf)
    .filter(Boolean);
  if (!projects.includes(opts["--expected-project"])) {
    finish(
      1,
      "mismatch",
      "artifact-mismatch",
      `no remote for project ${opts["--expected-project"]}`
    );
  }
}

if (existsSync(repoOut) && readdirSync(repoOut).length > 0) {
  finish(1, "mismatch", "repo-exists", repoOut);
}
try {
  mkdirSync(repoOut, { recursive: true });
  receipt.repo.created = true;
  const git = (args) =>
    execFileSync("git", args, {
      cwd: repoOut,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    }).trim();
  git(["init", "-q"]);
  git(["bundle", "verify", bundlePath]);
  git(["fetch", "-q", bundlePath, `${exported.refFull}:refs/heads/${branch}`]);
  const head = git(["rev-parse", `refs/heads/${branch}`]).toLowerCase();
  if (head !== tip)
    finish(
      1,
      "mismatch",
      "artifact-mismatch",
      `imported tip ${head} != ${tip}`
    );
  git(["checkout", "-q", "-B", branch, head]);
  const observed = git(["rev-parse", "--verify", "HEAD"]).toLowerCase();
  if (observed !== tip)
    finish(1, "mismatch", "artifact-mismatch", `HEAD ${observed} != ${tip}`);
  receipt.repo.head = observed;
} catch (error) {
  finish(1, "mismatch", "git-failed", String(error.message).slice(0, 400));
}

finish(0, "imported", "imported");
