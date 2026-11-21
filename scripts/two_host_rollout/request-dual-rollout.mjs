#!/usr/bin/env node
// Dell side: build the dual-repo Lenovo job for issue #162. Files only:
// one request JSON plus one command file for the standard signed client.
// Never submits, never touches the queue, never carries secrets.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { DUAL_REQUEST_SCHEMA } from "./dual-lib.mjs";
import {
  SHA_RE,
  assertNoSecrets,
  assertSafeCommandPart,
  buildForTransport,
} from "./lib.mjs";

const opts = {};
for (let i = 0; i < process.argv.length; i += 1) {
  const key = process.argv[i];
  if (
    ![
      "--node",
      "--lenovo-checkout",
      "--lenovo-skills-dir",
      "--lenovo-pat-checkout",
      "--lenovo-pat-skills-dir",
      "--task-runner-sha",
      "--pat-sha",
      "--is-latest",
      "--request-name",
      "--command-file",
      "--shared",
    ].includes(key)
  )
    continue;
  opts[key] = process.argv[i + 1];
  i += 1;
}

function refuse(message) {
  process.stderr.write(`two-host-rollout request-dual-rollout: ${message}\n`);
  process.stderr.write(
    "usage: request-dual-rollout.mjs --node <abs> --lenovo-checkout <abs> --lenovo-skills-dir <abs> --lenovo-pat-checkout <abs> --lenovo-pat-skills-dir <abs> --task-runner-sha <sha> --pat-sha <sha> --is-latest true|false --request-name <file> --command-file <file> --shared <dir>\n"
  );
  process.exit(2);
}

for (const key of [
  "--node",
  "--lenovo-checkout",
  "--lenovo-skills-dir",
  "--lenovo-pat-checkout",
  "--lenovo-pat-skills-dir",
  "--task-runner-sha",
  "--pat-sha",
  "--is-latest",
  "--request-name",
  "--command-file",
  "--shared",
]) {
  if (!opts[key]) refuse(`${key} is required`);
}
const trt = String(opts["--task-runner-sha"]).toLowerCase();
const pt = String(opts["--pat-sha"]).toLowerCase();
if (!SHA_RE.test(trt)) refuse("--task-runner-sha must be a full commit SHA");
if (!SHA_RE.test(pt)) refuse("--pat-sha must be a full commit SHA");
if (!["true", "false"].includes(opts["--is-latest"]))
  refuse("--is-latest must be true|false");
for (const key of [
  "--node",
  "--lenovo-checkout",
  "--lenovo-skills-dir",
  "--lenovo-pat-checkout",
  "--lenovo-pat-skills-dir",
]) {
  if (!isAbsolute(opts[key])) refuse(`${key} must be absolute on Lenovo`);
}
if (/[\\/]/.test(opts["--request-name"]) || opts["--request-name"].startsWith("."))
  refuse("--request-name must be one contained file name");
const shared = resolve(opts["--shared"]);
if (!existsSync(shared)) refuse(`shared directory does not exist: ${shared}`);
for (const [label, value] of [
  ["--node", opts["--node"]],
  ["--lenovo-checkout", opts["--lenovo-checkout"]],
  ["--lenovo-skills-dir", opts["--lenovo-skills-dir"]],
  ["--lenovo-pat-checkout", opts["--lenovo-pat-checkout"]],
  ["--lenovo-pat-skills-dir", opts["--lenovo-pat-skills-dir"]],
  ["--command-file", opts["--command-file"]],
]) {
  try {
    assertSafeCommandPart(label, value);
  } catch (error) {
    refuse(error.message);
  }
}
try {
  assertNoSecrets("request", Object.values(opts).join(" "));
} catch (error) {
  refuse(error.message);
}

const request = {
  schema: DUAL_REQUEST_SCHEMA,
  taskRunnerSha: trt,
  patSha: pt,
  isLatest: opts["--is-latest"] === "true",
  checkout: opts["--lenovo-checkout"],
  skillsDir: opts["--lenovo-skills-dir"],
  patCheckout: opts["--lenovo-pat-checkout"],
  patSkillsDir: opts["--lenovo-pat-skills-dir"],
};
const text = `${JSON.stringify(request)}\n`;
const requestPath = join(shared, opts["--request-name"]);
mkdirSync(shared, { recursive: true });
writeFileSync(requestPath, text);
const digest = createHash("sha256").update(Buffer.from(text, "utf8")).digest("hex");
const argv = [
  opts["--node"],
  `${opts["--lenovo-checkout"]}/scripts/two_host_rollout/apply-dual-host.mjs`,
  "--checkout",
  opts["--lenovo-checkout"],
  "--target-sha",
  trt,
  "--is-latest",
  opts["--is-latest"],
  "--skills-dir",
  opts["--lenovo-skills-dir"],
  "--pat-checkout",
  opts["--lenovo-pat-checkout"],
  "--pat-sha",
  pt,
  "--pat-skills-dir",
  opts["--lenovo-pat-skills-dir"],
  "--receipt",
  `${opts["--lenovo-checkout"]}/../dual-rollout-${trt.slice(0, 12)}-${pt.slice(0, 12)}.receipt.json`,
];
let command;
try {
  command = buildForTransport(argv);
} catch (error) {
  refuse(error.message);
}
assertNoSecrets("command file", command);
writeFileSync(opts["--command-file"], `${command}\n`);
console.log(JSON.stringify({ requestPath, requestSha256: digest, command }));
