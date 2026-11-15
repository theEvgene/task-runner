#!/usr/bin/env node
import { cp, mkdir, rename, rm } from "node:fs/promises";
import { randomBytes } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const skillName = "create-dco-feature-flags-protocol";
const source = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "skills",
  skillName
);

function targetRoot(argv) {
  if (argv.length !== 2 || argv[0] !== "--target-root" || !argv[1])
    throw new Error(
      "usage: install-dco-feature-flags-protocol-skill.mjs --target-root <skills-directory>"
    );
  return resolve(argv[1]);
}

const root = targetRoot(process.argv.slice(2));
const destination = join(root, skillName);
const staging = join(
  root,
  `.${skillName}.install-${randomBytes(8).toString("hex")}`
);

await mkdir(root, { recursive: true });
try {
  await cp(source, staging, { recursive: true, errorOnExist: true });
  await rename(staging, destination);
} catch (error) {
  await rm(staging, { recursive: true, force: true });
  if (["EEXIST", "ENOTEMPTY", "EPERM"].includes(error.code))
    throw new Error(`skill destination already exists: ${destination}`);
  throw error;
}
process.stdout.write(`${destination}\n`);
