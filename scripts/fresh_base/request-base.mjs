#!/usr/bin/env node
// Dell side: write one base request plus its command file. Files only: never
// submits, never contacts Lenovo, never touches a remote. Only the request's
// SHA-256 travels inside the signed job; Lenovo re-hashes and refuses
// anything the job did not name.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import {
  BRANCH_RE,
  TOKEN_RE,
  fail,
  parseArgs,
  safeName,
  BASE_REQUEST_SCHEMA,
} from "./lib.mjs";

export { BASE_REQUEST_SCHEMA };

const opts = parseArgs(process.argv.slice(2), {
  "--shared": "required",
  "--token": "required",
  "--lenovo-skill-dir": "required",
  "--node": "required",
  "--lenovo-checkout": "required",
  "--ref-full": "required",
  "--request-name": "required",
  "--command-file": "required",
  "--repo": "optional",
});

function refuse(message) {
  fail(message);
  process.exit(1);
}

if (!TOKEN_RE.test(opts["--token"] ?? ""))
  refuse("--token must be 16 lowercase hex");
const branch = (opts["--ref-full"] ?? "").replace(/^refs\/heads\//, "");
if (
  !(opts["--ref-full"] ?? "").startsWith("refs/heads/") ||
  !BRANCH_RE.test(branch)
) {
  refuse("--ref-full must be refs/heads/<branch>");
}
for (const [label, value] of [
  ["--lenovo-skill-dir", opts["--lenovo-skill-dir"]],
  ["--node", opts["--node"]],
  ["--lenovo-checkout", opts["--lenovo-checkout"]],
]) {
  if (!isAbsolute(value ?? "")) refuse(`${label} must be absolute on Lenovo`);
}
if (!safeName(opts["--request-name"] ?? ""))
  refuse("--request-name must be one contained file name");
const shared = resolve(opts["--shared"]);
if (!existsSync(shared)) refuse(`shared directory does not exist: ${shared}`);
if (
  opts["--repo"] &&
  resolve(opts["--repo"]).toLowerCase() === shared.toLowerCase()
) {
  refuse("shared directory must stay outside the checkout");
}
if (opts["--repo"]) {
  const rel = shared
    .toLowerCase()
    .startsWith(`${resolve(opts["--repo"]).toLowerCase()}/`);
  if (rel) refuse("shared directory must stay outside the checkout");
}

const request = {
  schema: BASE_REQUEST_SCHEMA,
  token: opts["--token"],
  shared,
  lenovoCheckout: opts["--lenovo-checkout"],
  refFull: opts["--ref-full"],
  bundleName: `fresh-base-${opts["--token"]}.bundle`,
  manifestName: `fresh-base-${opts["--token"]}.manifest.json`,
};
const text = `${JSON.stringify(request)}\n`;
const requestPath = join(shared, opts["--request-name"]);
mkdirSync(shared, { recursive: true });
writeFileSync(requestPath, text);

const digest = createHash("sha256")
  .update(Buffer.from(text, "utf8"))
  .digest("hex");
const driver = join(
  opts["--lenovo-skill-dir"],
  "scripts",
  "fresh_base",
  "lenovo-base-export.mjs"
);
const command = `"${opts["--node"]}" "${driver}" --request "${requestPath}" --request-sha256 ${digest}`;
writeFileSync(opts["--command-file"], `${command}\n`);

console.log(JSON.stringify({ requestPath, requestSha256: digest, command }));
