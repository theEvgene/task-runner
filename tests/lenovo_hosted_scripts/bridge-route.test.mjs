import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = fileURLToPath(new URL("../../", import.meta.url));
const dco = join(
  root,
  "skills",
  "create-dco-feature-flags-protocol",
  "scripts",
  "dco-feature-flags.mjs"
);

function run(args, cwd) {
  return spawnSync(process.execPath, [dco, ...args], {
    encoding: "utf8",
    cwd: cwd ?? root,
  });
}

test("DCO plan --description and --description-file are mutually exclusive", () => {
  const result = run([
    "plan",
    "--application",
    "l-loans",
    "--key",
    "bridge_probe",
    "--description",
    "x",
    "--description-file",
    "y",
  ]);
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}${result.stderr}`, /mutually exclusive/);
});

test("DCO plan file form preserves quotes, Unicode, and multiline", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "dco-desc-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const description = 'Flag "bridge" & <test>\nsecond line éß 中文';
  const file = join(dir, "desc.txt");
  await writeFile(file, description, "utf8");
  const fromFile = run([
    "plan",
    "--application",
    "l-loans",
    "--key",
    "bridge_probe",
    "--description-file",
    file,
  ]);
  assert.equal(fromFile.status, 0, fromFile.stderr || fromFile.stdout);
  const inline = run([
    "plan",
    "--application",
    "l-loans",
    "--key",
    "bridge_probe",
    "--description",
    description,
  ]);
  assert.equal(inline.status, 0, inline.stderr || inline.stdout);
  const filePlan = JSON.parse(fromFile.stdout);
  const inlinePlan = JSON.parse(inline.stdout);
  assert.equal(filePlan.planDigest, inlinePlan.planDigest);
  assert.equal(filePlan.description, description);
});

test("bridge doc is the single generic route and stays username-free", async () => {
  const doc = await readFile(
    join(root, "docs", "lenovo-hosted-scripts.md"),
    "utf8"
  );
  for (const needle of [
    "lan_bridge/lan_bridge.py",
    "lan_bridge/get.py",
    "docs/lan-bridge.md",
    "JIRA_SESSION_COOKIE",
    "DCO_TEST_ACCESS_TOKEN",
    "--comment-file",
    "--description-file",
    "--confirm",
    "same-id",
    "never",
  ])
    assert.match(doc, new RegExp(needle.replace(/[./-]/g, "\\$&")));
  assert.doesNotMatch(doc, /C:\\Users\\/);
  assert.doesNotMatch(doc, /jira-task\.py read/);
  for (const needle of [
    "has no `Duplicate` state",
    "possibly-unknown",
    "single unverified observation",
    "never infer the exact CLI code survived",
    "unparsable",
    "never a mutation retry",
  ])
    assert.match(doc, new RegExp(needle.replace(/[./-]/g, "\\$&")));
  const bridge = await readFile(join(root, "docs", "lan-bridge.md"), "utf8");
  assert.match(bridge, /error.*stdout receipt/s);
  const ops = await readFile(
    join(root, "scripts", "jira_protocol", "operations.mjs"),
    "utf8"
  );
  assert.doesNotMatch(ops, /Duplicate/);
  const dcoSkill = await readFile(
    join(root, "skills", "create-dco-feature-flags-protocol", "SKILL.md"),
    "utf8"
  );
  assert.match(dcoSkill, /lenovo-hosted-scripts\.md/);
  assert.match(dcoSkill, /--description-file/);
});
