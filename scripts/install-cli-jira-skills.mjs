#!/usr/bin/env node
import { createHash, randomBytes } from "node:crypto";
import {
  cp,
  mkdir,
  readFile,
  rename,
  rm,
  stat,
  writeFile,
} from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const skillNames = [
  "cli-create-lel-service-backlog-issue",
  "cli-assign-jira-issue",
  "cli-transition-jira-issue",
  "cli-transition-jira-status",
  "cli-extract-jira-content",
  "cli-flag-jira-issue",
  "cli-create-jira-subtask",
  "cli-create-jira-subtasks",
];
const runtimeFiles = [
  "scripts/jira-protocol.mjs",
  "scripts/jira_protocol/protocol.mjs",
  "scripts/jira_protocol/operations.mjs",
  "scripts/jira_protocol/route.mjs",
  "scripts/jira_protocol/batch.mjs",
  "scripts/jira_protocol/batch-parse.mjs",
  "scripts/jira_protocol/batch-journal.mjs",
  "scripts/jira_protocol/batch-plan.mjs",
  "scripts/jira_protocol/batch-execute.mjs",
  "scripts/jira_protocol/handoff.mjs",
  "scripts/jira_protocol/extract.mjs",
  "scripts/jira_protocol/html-form.mjs",
  "docs/jira-protocol.md",
  "docs/jira-protocol-batch.md",
  "docs/jira-protocol-handoff.md",
];

function usage() {
  throw new Error(
    "usage: install-cli-jira-skills.mjs --target-root <skills-directory> [--runtime-root <task-runner-checkout>]"
  );
}

function parse(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || !value || value.startsWith("--")) usage();
    if (!new Set(["--target-root", "--runtime-root"]).has(name)) usage();
    if (Object.hasOwn(values, name)) usage();
    values[name] = value;
  }
  if (!values["--target-root"]) usage();
  return {
    targetRoot: resolve(values["--target-root"]),
    runtimeRoot: resolve(values["--runtime-root"] ?? repositoryRoot),
  };
}

async function exists(path) {
  try {
    await stat(path);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

async function digest(path) {
  return createHash("sha256")
    .update(await readFile(path))
    .digest("hex");
}

const { targetRoot, runtimeRoot } = parse(process.argv.slice(2));
const sources = Object.fromEntries(
  runtimeFiles.map((relative) => [relative, join(runtimeRoot, relative)])
);
for (const [relative, source] of Object.entries(sources)) {
  if (!(await exists(source)))
    throw new Error(`Jira protocol runtime is incomplete: missing ${relative}`);
}

await mkdir(targetRoot, { recursive: true });
for (const skillName of skillNames) {
  if (await exists(join(targetRoot, skillName)))
    throw new Error(
      `skill destination already exists: ${join(targetRoot, skillName)}`
    );
}

const nonce = randomBytes(8).toString("hex");
const staged = [];
const installed = [];
try {
  const hashes = {};
  for (const [relative, source] of Object.entries(sources)) {
    hashes[relative] = await digest(source);
  }
  hashes["references/jira-protocol.md"] = hashes["docs/jira-protocol.md"];
  hashes["references/jira-protocol-batch.md"] =
    hashes["docs/jira-protocol-batch.md"];
  hashes["references/jira-protocol-handoff.md"] =
    hashes["docs/jira-protocol-handoff.md"];
  delete hashes["docs/jira-protocol.md"];
  delete hashes["docs/jira-protocol-batch.md"];
  delete hashes["docs/jira-protocol-handoff.md"];
  for (const skillName of skillNames) {
    const stage = join(targetRoot, `.${skillName}.install-${nonce}`);
    staged.push(stage);
    await cp(join(repositoryRoot, "skills", skillName), stage, {
      recursive: true,
      errorOnExist: true,
    });
    await mkdir(join(stage, "scripts", "jira_protocol"), { recursive: true });
    await mkdir(join(stage, "references"), { recursive: true });
    await cp(
      sources["scripts/jira-protocol.mjs"],
      join(stage, "scripts", "jira-protocol.mjs")
    );
    for (const moduleName of [
      "protocol.mjs",
      "operations.mjs",
      "route.mjs",
      "batch.mjs",
      "batch-parse.mjs",
      "batch-journal.mjs",
      "batch-plan.mjs",
      "batch-execute.mjs",
      "handoff.mjs",
      "extract.mjs",
      "html-form.mjs",
    ]) {
      await cp(
        sources[`scripts/jira_protocol/${moduleName}`],
        join(stage, "scripts", "jira_protocol", moduleName)
      );
    }
    await cp(
      sources["docs/jira-protocol.md"],
      join(stage, "references", "jira-protocol.md")
    );
    await cp(
      sources["docs/jira-protocol-batch.md"],
      join(stage, "references", "jira-protocol-batch.md")
    );
    await cp(
      sources["docs/jira-protocol-handoff.md"],
      join(stage, "references", "jira-protocol-handoff.md")
    );
    await writeFile(
      join(stage, "runtime-manifest.json"),
      `${JSON.stringify({ schema: "cli-jira-skill-runtime/1", files: hashes }, null, 2)}\n`,
      "utf8"
    );
  }
  for (let index = 0; index < skillNames.length; index += 1) {
    const destination = join(targetRoot, skillNames[index]);
    await rename(staged[index], destination);
    installed.push(destination);
    if (
      process.env.NODE_ENV === "test" &&
      process.env.JIRA_SKILL_INSTALL_FAIL_AFTER === String(index + 1)
    )
      throw new Error("injected installer failure");
  }
} catch (error) {
  for (const path of [...staged, ...installed]) {
    await rm(path, { recursive: true, force: true });
  }
  throw error;
}

process.stdout.write(`${JSON.stringify({ installed }, null, 2)}\n`);
