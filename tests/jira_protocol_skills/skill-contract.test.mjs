import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = new URL("../../", import.meta.url);
const rootPath = fileURLToPath(root);
const skill = (name, file = "SKILL.md") =>
  new URL(`skills/${name}/${file}`, root);

const names = [
  "cli-create-lel-service-backlog-issue",
  "cli-assign-jira-issue",
  "cli-transition-jira-issue",
  "cli-transition-jira-status",
  "cli-extract-jira-content",
  "cli-find-sibling-issues",
  "cli-flag-jira-issue",
  "cli-create-jira-subtask",
  "cli-create-jira-subtasks",
];

test("all Jira protocol skills are CLI-prefixed and use the installed runtime", async () => {
  for (const name of names) {
    const text = await readFile(skill(name), "utf8");
    assert.match(text, new RegExp(`^---\\r?\\nname: ${name}\\r?\\n`));
    assert.match(text, /scripts\/jira-protocol\.mjs/);
    assert.match(text, /references\/jira-protocol\.md/);
    assert.match(text, /relative to this `SKILL\.md`/);
    assert.match(text, /401\/403/);
    assert.match(text, /lenovo-hosted-scripts\.md/);
  }
});

test("creation preserves duplicate, confirmation, readback, and recovery boundaries", async () => {
  const text = await readFile(skill(names[0]), "utf8");
  assert.match(text, /duplicate search: unavailable in this CLI/);
  assert.match(text, /MissingRequiredFields/);
  assert.match(text, /--confirm <confirmation>/);
  assert.match(text, /persistedFields/);
  assert.match(text, /rapid view `298290`/);
  assert.match(text, /UnknownOutcome/);
  assert.match(text, /stop without another create/);
});

test("assignment requires an exact login and authoritative readback", async () => {
  const text = await readFile(skill(names[1]), "utf8");
  assert.match(text, /no current-user lookup/);
  assert.match(text, /JIRA_ATL_TOKEN/);
  assert.match(text, /direct\s+issue-by-key readback/);
  assert.match(text, /exposes no safe mutation retry/);
});

test("transition does not invent workflow paths, columns, or form submission", async () => {
  const text = await readFile(skill(names[2]), "utf8");
  assert.match(text, /applies one/);
  assert.match(text, /does not synthesize a path/);
  assert.match(text, /does not expose board-column discovery/);
  assert.match(text, /hasTransitionView=true/);
  assert.match(text, /Never\s+repeat the mutation blindly/);
});

test("status route uses only live edges and re-discovers every step", async () => {
  const text = await readFile(skill(names[3]), "utf8");
  assert.match(
    text,
    /jira-protocol\.mjs transition-to-status[\s\\]+--issue LEL-1234/
  );
  assert.match(text, /--confirm <confirmation>/);
  assert.match(text, /never synthesizes a path from a static map/);
  assert.match(text, /re-discovers before every step/);
  assert.match(text, /UnknownOutcome/);
  assert.match(text, /never repeat the mutation/);
  assert.match(text, /401\/403/);
  assert.match(text, /lenovo-hosted-scripts\.md/);
});

test("extract binds the REST command, completeness ledger, and external gaps", async () => {
  const text = await readFile(skill(names[4]), "utf8");
  assert.match(text, /jira-protocol\.mjs extract --issue LEL-1234/);
  assert.match(text, /completeness\.comprehensive/);
  assert.match(text, /parent\.presence: unobserved/);
  assert.match(text, /not_found_or_not_visible/);
  assert.match(text, /fullContentProven/);
  assert.match(text, /Wiki pages/);
  assert.match(text, /attachment bodies/);
  assert.match(text, /HTTP 401\/403 is terminal/);
});

test("sibling search binds exact assignee, short entries, and incompleteness", async () => {
  const text = await readFile(skill(names[5]), "utf8");
  assert.match(text, /jira-protocol\.mjs siblings --issue LEL-1234/);
  assert.match(text, /--assignee/);
  assert.match(text, /never listed as its own sibling/);
  assert.match(text, /assignee\.name/);
  assert.match(text, /assignee\.key/);
  assert.match(text, /unassigned/);
  assert.match(text, /unreadable/);
  assert.match(text, /access_denied/);
  assert.match(text, /not_found_or_not_visible/);
  assert.match(text, /completeness\.comprehensive/);
  assert.match(text, /401\/403 on the source read is terminal/);
  assert.match(text, /never merge these short entries/);
});

test("flag binds the required comment, refusal replay, and recovery boundary", async () => {
  const text = await readFile(skill(names[6]), "utf8");
  assert.match(text, /jira-protocol\.mjs flag[\s\\]+--issue LEL-1234/);
  assert.match(text, /--comment-file/);
  assert.match(text, /--confirm <confirmation>/);
  assert.match(text, /readback\.flag/);
  assert.match(text, /readback\.commentId/);
  assert.match(text, /UnknownOutcome/);
  assert.match(text, /never repeat the mutation/);
  assert.match(text, /lenovo-hosted-scripts\.md/);
});

test("subtask binds parent linkage, confirmation, readback, and recovery boundary", async () => {
  const text = await readFile(skill(names[7]), "utf8");
  assert.match(text, /duplicate search: unavailable in this CLI/);
  assert.match(text, /MissingRequiredFields/);
  assert.match(text, /--confirm <confirmation>/);
  assert.match(text, /persistedFields/);
  assert.match(text, /parentKey/);
  assert.match(text, /UnknownOutcome/);
  assert.match(text, /stop without another create/);
});

test("subtask batch matches live children and resumes without duplicates", async () => {
  const text = await readFile(skill(names[8]), "utf8");
  assert.match(
    text,
    /jira-protocol\.mjs create-subtasks[\s\\]+--parent LEL-1234/
  );
  assert.match(text, /--confirm <confirmation>/);
  assert.match(text, /--allow-duplicate/);
  assert.match(text, /UnknownOutcome/);
  assert.match(text, /AlreadyExists/);
  assert.match(text, /parentKey/);
  assert.match(text, /401\/403/);
  assert.match(text, /lenovo-hosted-scripts\.md/);
});

test("installer source names the actual runtime and records its hashes", async () => {
  const text = await readFile(
    new URL("scripts/install-cli-jira-skills.mjs", root),
    "utf8"
  );
  for (const name of names) assert.match(text, new RegExp(`"${name}"`));
  for (const file of [
    "scripts/jira-protocol.mjs",
    "scripts/jira_protocol/protocol.mjs",
    "scripts/jira_protocol/operations.mjs",
    "scripts/jira_protocol/route.mjs",
    "scripts/jira_protocol/batch.mjs",
    "scripts/jira_protocol/handoff.mjs",
    "scripts/jira_protocol/extract.mjs",
    "scripts/jira_protocol/siblings.mjs",
    "scripts/jira_protocol/html-form.mjs",
    "docs/jira-protocol.md",
    "docs/jira-protocol-handoff.md",
  ]) {
    assert.match(text, new RegExp(file.replace(/[./-]/g, "\\$&")));
  }
  assert.match(text, /sha256/);
  assert.match(text, /cli-jira-skill-runtime\/1/);
});

test("installer copies a real runtime, verifies hashes, and leaves runnable entries", async (t) => {
  const runtimeRoot = process.env.JIRA_PROTOCOL_RUNTIME_ROOT ?? rootPath;
  try {
    await readFile(join(runtimeRoot, "scripts", "jira-protocol.mjs"));
  } catch {
    t.skip(
      "set JIRA_PROTOCOL_RUNTIME_ROOT to the Jira protocol checkout for cross-worktree integration"
    );
    return;
  }
  const temporary = await mkdtemp(join(tmpdir(), "jira-skills-install-"));
  const target = join(temporary, "skills");
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const install = spawnSync(
    process.execPath,
    [
      join(rootPath, "scripts", "install-cli-jira-skills.mjs"),
      "--target-root",
      target,
      "--runtime-root",
      runtimeRoot,
    ],
    { cwd: rootPath, encoding: "utf8" }
  );
  assert.equal(install.status, 0, install.stderr || install.stdout);

  for (const name of names) {
    const installed = join(target, name);
    const manifest = JSON.parse(
      await readFile(join(installed, "runtime-manifest.json"), "utf8")
    );
    assert.equal(manifest.schema, "cli-jira-skill-runtime/1");
    for (const [relative, expected] of Object.entries(manifest.files)) {
      const actual = createHash("sha256")
        .update(await readFile(join(installed, relative)))
        .digest("hex");
      assert.equal(actual, expected, `${name}/${relative}`);
    }
    const entry = spawnSync(
      process.execPath,
      [join(installed, "scripts", "jira-protocol.mjs")],
      { cwd: temporary, encoding: "utf8" }
    );
    assert.equal(entry.status, 2);
    assert.match(entry.stdout, /usage: jira-protocol/);
    assert.doesNotMatch(
      `${entry.stdout}${entry.stderr}`,
      /ERR_MODULE_NOT_FOUND/
    );
  }
});

test("installer refuses existing destinations before copying", async (t) => {
  const runtimeRoot = process.env.JIRA_PROTOCOL_RUNTIME_ROOT ?? rootPath;
  try {
    await readFile(join(runtimeRoot, "scripts", "jira-protocol.mjs"));
  } catch {
    t.skip(
      "set JIRA_PROTOCOL_RUNTIME_ROOT to the Jira protocol checkout for cross-worktree integration"
    );
    return;
  }
  const temporary = await mkdtemp(join(tmpdir(), "jira-skills-existing-"));
  const target = join(temporary, "skills");
  await mkdir(join(target, names[0]), { recursive: true });
  try {
    const result = spawnSync(
      process.execPath,
      [
        join(rootPath, "scripts", "install-cli-jira-skills.mjs"),
        "--target-root",
        target,
        "--runtime-root",
        runtimeRoot,
      ],
      { cwd: rootPath, encoding: "utf8" }
    );
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /skill destination already exists/);
    for (const name of names.slice(1))
      await assert.rejects(readFile(join(target, name, "SKILL.md")), /ENOENT/);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("installer rolls back every destination after a partial rename failure", async (t) => {
  const runtimeRoot = process.env.JIRA_PROTOCOL_RUNTIME_ROOT ?? rootPath;
  try {
    await readFile(join(runtimeRoot, "scripts", "jira-protocol.mjs"));
  } catch {
    t.skip(
      "set JIRA_PROTOCOL_RUNTIME_ROOT to the Jira protocol checkout for cross-worktree integration"
    );
    return;
  }
  const temporary = await mkdtemp(join(tmpdir(), "jira-skills-rollback-"));
  const target = join(temporary, "skills");
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const result = spawnSync(
    process.execPath,
    [
      join(rootPath, "scripts", "install-cli-jira-skills.mjs"),
      "--target-root",
      target,
      "--runtime-root",
      runtimeRoot,
    ],
    {
      cwd: rootPath,
      encoding: "utf8",
      env: {
        ...process.env,
        NODE_ENV: "test",
        JIRA_SKILL_INSTALL_FAIL_AFTER: "2",
      },
    }
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /injected installer failure/);
  for (const name of names)
    await assert.rejects(readFile(join(target, name, "SKILL.md")), /ENOENT/);
});
