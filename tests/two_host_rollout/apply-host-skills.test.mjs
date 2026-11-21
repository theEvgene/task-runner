import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readlinkSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const applyHost = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "apply-host.mjs"
);

const RUNTIME_FILES = [
  "scripts/jira-protocol.mjs",
  "scripts/jira_protocol/protocol.mjs",
  "scripts/jira_protocol/operations.mjs",
  "scripts/jira_protocol/operations-shared.mjs",
  "scripts/jira_protocol/operations-transition.mjs",
  "scripts/jira_protocol/operations-assign.mjs",
  "scripts/jira_protocol/operations-epic.mjs",
  "scripts/jira_protocol/operations-subtask.mjs",
  "scripts/jira_protocol/operations-flag.mjs",
  "scripts/jira_protocol/route.mjs",
  "scripts/jira_protocol/batch.mjs",
  "scripts/jira_protocol/batch-parse.mjs",
  "scripts/jira_protocol/batch-journal.mjs",
  "scripts/jira_protocol/batch-plan.mjs",
  "scripts/jira_protocol/batch-execute.mjs",
  "scripts/jira_protocol/handoff.mjs",
  "scripts/jira_protocol/extract.mjs",
  "scripts/jira_protocol/siblings.mjs",
  "scripts/jira_protocol/read-fields.mjs",
  "scripts/jira_protocol/html-form.mjs",
  "docs/jira-protocol.md",
  "docs/jira-protocol-subtasks.md",
  "docs/jira-protocol-batch.md",
  "docs/jira-protocol-handoff.md",
  "docs/jira-protocol-read-fields.md",
];
const SKILL_DIRS = [
  "cli-create-lel-service-backlog-issue",
  "cli-assign-jira-issue",
  "cli-transition-jira-issue",
  "cli-transition-jira-status",
  "cli-extract-jira-content",
  "cli-find-sibling-issues",
  "cli-read-selected-fields",
  "cli-flag-jira-issue",
  "cli-create-jira-subtask",
  "cli-create-jira-subtasks",
];

function git(args, cwd) {
  return execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
}

function commitAs(repo, message) {
  git(
    [
      "-c",
      "user.name=t",
      "-c",
      "user.email=t@t",
      "commit",
      "-q",
      "-m",
      message,
    ],
    repo
  );
}

function runApply(args) {
  try {
    const out = execFileSync("node", [applyHost, ...args], {
      encoding: "utf8",
    });
    return { code: 0, json: JSON.parse(out) };
  } catch (error) {
    const text = `${error.stdout ?? ""}`;
    const line = text.trim().split("\n").pop() ?? "";
    return { code: error.status ?? 1, json: JSON.parse(line), raw: text };
  }
}

// One bare origin plus one clone holding the committed runtime. Fast local
// git only.
function makeCheckout() {
  const root = mkdtempSync(join(tmpdir(), "two-host-"));
  const remote = join(root, "origin.git");
  git(["init", "--bare", "--initial-branch=main", "-q", remote], root);
  const work = join(root, "work");
  git(["clone", "-q", remote, work], root);
  git(["checkout", "-q", "-b", "main"], work);
  for (const relative of RUNTIME_FILES) {
    const src = join(repoRoot, relative);
    const dst = join(work, relative);
    mkdirSync(dirname(dst), { recursive: true });
    cpSync(src, dst);
  }
  for (const name of SKILL_DIRS) {
    mkdirSync(join(repoRoot, "skills", name), { recursive: true });
  }
  git(["add", "-A"], work);
  commitAs(work, "runtime");
  git(["push", "-q", "origin", "main"], work);
  return { root, remote, work };
}

// Issue #159: managed junctions (release-installer model) are recognized,
// follow the checkout across revisions with no reinstall, and rerun safe.
function commitSkillShells(work) {
  for (const name of SKILL_DIRS) {
    mkdirSync(join(work, "skills", name), { recursive: true });
    writeFileSync(join(work, "skills", name, "SKILL.md"), `# ${name}\n`);
  }
  git(["add", "-A"], work);
  commitAs(work, "skill shells");
  git(["push", "-q", "origin", "main"], work);
}

function linkJunctionSkills(work, skills) {
  mkdirSync(skills, { recursive: true });
  for (const name of SKILL_DIRS) {
    symlinkSync(join(work, "skills", name), join(skills, name), "junction");
  }
}

test("managed junctions update with the checkout and rerun is already-current", () => {
  const { work } = makeCheckout();
  commitSkillShells(work);
  writeFileSync(join(work, "v2.txt"), "v2\n");
  git(["add", "-A"], work);
  commitAs(work, "v2");
  git(["push", "-q", "origin", "main"], work);
  const target = git(["rev-parse", "HEAD"], work);
  git(["reset", "-q", "--hard", "HEAD~1"], work);
  const skills = join(work, "..", "skills");
  linkJunctionSkills(work, skills);
  const receipt = join(work, "..", "dell.json");
  const r = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    receipt,
  ]);
  assert.equal(r.code, 0, JSON.stringify(r.json));
  assert.equal(r.json.verdict, "updated");
  assert.equal(r.json.skills.state, "current");
  assert.equal(r.json.skills.model, "junction");
  for (const name of SKILL_DIRS) {
    assert.ok(readlinkSync(join(skills, name)).length > 0);
    assert.equal(existsSync(join(skills, name, "runtime-manifest.json")), false);
  }
  const second = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    receipt,
  ]);
  assert.equal(second.code, 0);
  assert.equal(second.json.verdict, "already-current");
});

test("foreign link refuses ownership-conflict before any mutation", () => {
  const { work } = makeCheckout();
  commitSkillShells(work);
  const target = git(["rev-parse", "HEAD"], work);
  const beforeHead = git(["rev-parse", "HEAD"], work);
  const skills = join(work, "..", "skills");
  mkdirSync(skills, { recursive: true });
  const elsewhere = mkdtempSync(join(tmpdir(), "two-host-foreign-"));
  writeFileSync(join(elsewhere, "SKILL.md"), "foreign\n");
  symlinkSync(elsewhere, join(skills, SKILL_DIRS[0]), "junction");
  const r = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    join(work, "..", "r.json"),
  ]);
  assert.equal(r.code, 1);
  assert.equal(r.json.reason, "ownership-conflict");
  assert.match(r.json.detail, /foreign link/);
  assert.equal(git(["rev-parse", "HEAD"], work), beforeHead);
  assert.equal(readlinkSync(join(skills, SKILL_DIRS[0])), elsewhere);
});

// Issue #159: proven stale copies update through the compatible reinstall
// route and rerun is already-current.
test("stale managed copies reinstall after the checkout moves", () => {
  const { work } = makeCheckout();
  const firstTarget = git(["rev-parse", "HEAD"], work);
  const skills = join(work, "..", "skills");
  const receipt = join(work, "..", "r.json");
  const installed = runApply([
    "--checkout",
    work,
    "--target-sha",
    firstTarget,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    receipt,
  ]);
  assert.equal(installed.code, 0, JSON.stringify(installed.json));
  assert.equal(installed.json.skills.model, "copy");
  writeFileSync(
    join(work, RUNTIME_FILES[0]),
    `${readFileSync(join(work, RUNTIME_FILES[0]), "utf8")}\n// v2\n`
  );
  git(["add", "-A"], work);
  commitAs(work, "runtime v2");
  git(["push", "-q", "origin", "main"], work);
  const target = git(["rev-parse", "HEAD"], work);
  git(["reset", "-q", "--hard", "HEAD~1"], work);
  const moved = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    receipt,
  ]);
  assert.equal(moved.code, 0, JSON.stringify(moved.json));
  assert.equal(moved.json.verdict, "updated");
  const manifest = JSON.parse(
    readFileSync(join(skills, SKILL_DIRS[0], "runtime-manifest.json"), "utf8")
  );
  const currentHash = createHash("sha256")
    .update(readFileSync(join(work, RUNTIME_FILES[0])))
    .digest("hex");
  assert.equal(manifest.files[RUNTIME_FILES[0]], currentHash);
  const rerun = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    receipt,
  ]);
  assert.equal(rerun.code, 0);
  assert.equal(rerun.json.verdict, "already-current");
});

// Issue #159: a changed copy refuses before any overwrite.
test("changed copy refuses ownership-conflict and is left intact", () => {
  const { work } = makeCheckout();
  const target = git(["rev-parse", "HEAD"], work);
  const skills = join(work, "..", "skills");
  const installed = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    join(work, "..", "r.json"),
  ]);
  assert.equal(installed.code, 0, JSON.stringify(installed.json));
  const payload = join(skills, SKILL_DIRS[0], "scripts", "jira-protocol.mjs");
  writeFileSync(payload, "tampered\n");
  const r = runApply([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
    "--receipt",
    join(work, "..", "r2.json"),
  ]);
  assert.equal(r.code, 1);
  assert.equal(r.json.reason, "ownership-conflict");
  assert.match(r.json.detail, /changed copy/);
  assert.equal(readFileSync(payload, "utf8"), "tampered\n");
  assert.equal(
    git(["rev-parse", "HEAD"], work).toLowerCase(),
    target.toLowerCase()
  );
});
