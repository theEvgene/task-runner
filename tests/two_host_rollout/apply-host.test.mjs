import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
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
const verifyReadback = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "verify-readback.mjs"
);

const RUNTIME_FILES = [
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
const SKILL_DIRS = [
  "cli-create-lel-service-backlog-issue",
  "cli-assign-jira-issue",
  "cli-transition-jira-issue",
  "cli-transition-jira-status",
  "cli-extract-jira-content",
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

// One bare origin plus one clone holding the committed runtime. Extra
// commits model behind/diverged/dirty states. Fast local git only.
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

test("behind main fast-forwards and verifies branch, SHA, cleanliness", () => {
  const { work } = makeCheckout();
  writeFileSync(join(work, "v2.txt"), "v2\n");
  git(["add", "-A"], work);
  commitAs(work, "v2");
  git(["push", "-q", "origin", "main"], work);
  const target = git(["rev-parse", "HEAD"], work);
  git(["reset", "-q", "--hard", "HEAD~1"], work);
  const skills = join(work, "..", "skills");
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
  assert.equal(r.code, 0);
  assert.equal(r.json.verdict, "updated");
  assert.equal(git(["symbolic-ref", "--short", "HEAD"], work), "main");
  assert.equal(
    git(["rev-parse", "HEAD"], work).toLowerCase(),
    target.toLowerCase()
  );
  assert.equal(git(["status", "--porcelain"], work), "");
  assert.equal(r.json.clean, true);
  for (const name of SKILL_DIRS)
    assert.ok(existsSync(join(skills, name, "runtime-manifest.json")));
  assert.equal(
    JSON.parse(readFileSync(receipt, "utf8")).head.toLowerCase(),
    target.toLowerCase()
  );
});

test("clean detached HEAD on target ends on main without detached HEAD", () => {
  const { work } = makeCheckout();
  const target = git(["rev-parse", "HEAD"], work);
  git(["checkout", "-q", "--detach", target], work);
  const skills = join(work, "..", "skills");
  const receipt = join(work, "..", "host.json");
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
  assert.equal(r.code, 0);
  assert.equal(git(["symbolic-ref", "--short", "HEAD"], work), "main");
  assert.equal(
    git(["rev-parse", "HEAD"], work).toLowerCase(),
    target.toLowerCase()
  );
});

test("dirty checkout and diverged branch refuse with actual state", () => {
  const w1 = makeCheckout();
  writeFileSync(join(w1.work, "dirty.txt"), "x\n");
  const r1 = runApply([
    "--checkout",
    w1.work,
    "--target-sha",
    git(["rev-parse", "HEAD"], w1.work),
    "--is-latest",
    "true",
    "--skills-dir",
    join(w1.work, "..", "s"),
    "--receipt",
    join(w1.work, "..", "r.json"),
  ]);
  assert.equal(r1.code, 1);
  assert.equal(r1.json.reason, "dirty-checkout");

  const w2 = makeCheckout();
  const published = git(["rev-parse", "HEAD"], w2.work);
  writeFileSync(join(w2.work, "local.txt"), "local\n");
  git(["add", "-A"], w2.work);
  commitAs(w2.work, "local");
  const r2 = runApply([
    "--checkout",
    w2.work,
    "--target-sha",
    published,
    "--is-latest",
    "true",
    "--skills-dir",
    join(w2.work, "..", "s"),
    "--receipt",
    join(w2.work, "..", "r.json"),
  ]);
  assert.equal(r2.code, 1);
  assert.ok(["diverged", "revision-mismatch"].includes(r2.json.reason));
});

test("pinned older revision checks out detached and leaves main alone", () => {
  const { work } = makeCheckout();
  const old = git(["rev-parse", "HEAD"], work);
  writeFileSync(join(work, "new.txt"), "new\n");
  git(["add", "-A"], work);
  commitAs(work, "new");
  git(["push", "-q", "origin", "main"], work);
  const tip = git(["rev-parse", "HEAD"], work);
  const mainBefore = git(["rev-parse", "refs/heads/main"], work);
  assert.notEqual(old, tip);
  const skills = join(work, "..", "skills");
  const receipt = join(work, "..", "pinned.json");
  const r = runApply([
    "--checkout",
    work,
    "--target-sha",
    old,
    "--is-latest",
    "false",
    "--skills-dir",
    skills,
    "--receipt",
    receipt,
  ]);
  assert.equal(r.code, 0);
  assert.equal(
    git(["rev-parse", "HEAD"], work).toLowerCase(),
    old.toLowerCase()
  );
  assert.equal(git(["rev-parse", "refs/heads/main"], work), mainBefore);
  const v = (() => {
    try {
      const out = execFileSync(
        "node",
        [
          verifyReadback,
          "--dell-receipt",
          receipt,
          "--lenovo-receipt",
          receipt,
          "--target-sha",
          old,
          "--is-latest",
          "false",
        ],
        { encoding: "utf8" }
      );
      return JSON.parse(out);
    } catch (error) {
      return JSON.parse(`${error.stdout ?? ""}`);
    }
  })();
  assert.equal(v.verdict, "updated");
});

test("stale origin ref refuses a newer tip as latest until fetch proves freshness", () => {
  const made = makeCheckout();
  const { remote, work } = made;
  const root = made.root;
  // Advance the remote without touching the checkout: the local
  // origin/main ref goes stale, modelling a target branch that moved.
  const seed = join(root, "seed");
  git(["clone", "-q", remote, seed], root);
  writeFileSync(join(seed, "v2.txt"), "v2\n");
  git(["add", "-A"], seed);
  commitAs(seed, "v2");
  git(["push", "-q", "origin", "main"], seed);
  const tip = git(["rev-parse", "HEAD"], seed);
  // Stale checkout: the newer tip is not its origin/main, so latest
  // with that target refuses instead of passing off old state.
  const stale = runApply([
    "--checkout",
    work,
    "--target-sha",
    tip,
    "--is-latest",
    "true",
    "--skills-dir",
    join(work, "..", "s"),
    "--receipt",
    join(work, "..", "r.json"),
  ]);
  assert.equal(stale.code, 1);
  assert.equal(stale.json.reason, "revision-mismatch");
  // Freshness: fetch, then the same revision rolls forward on main.
  git(["fetch", "-q", "origin"], work);
  const receipt = join(work, "..", "fresh.json");
  const fresh = runApply([
    "--checkout",
    work,
    "--target-sha",
    tip,
    "--is-latest",
    "true",
    "--skills-dir",
    join(work, "..", "skills"),
    "--receipt",
    receipt,
  ]);
  assert.equal(fresh.code, 0, JSON.stringify(fresh.json));
  assert.equal(fresh.json.verdict, "updated");
  assert.equal(git(["symbolic-ref", "--short", "HEAD"], work), "main");
  assert.equal(
    git(["rev-parse", "HEAD"], work).toLowerCase(),
    tip.toLowerCase()
  );
});

test("foreign skill install refuses ownership-conflict and is left untouched", () => {
  const { work } = makeCheckout();
  const target = git(["rev-parse", "HEAD"], work);
  const skills = join(work, "..", "skills");
  const foreign = join(skills, SKILL_DIRS[0]);
  mkdirSync(foreign, { recursive: true });
  writeFileSync(join(foreign, "foreign.txt"), "not managed\n");
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
  assert.equal(readFileSync(join(foreign, "foreign.txt"), "utf8"), "not managed\n");
  assert.equal(existsSync(join(foreign, "runtime-manifest.json")), false);
});

test("success removes staging leftovers and rerun is already-current", () => {
  const { work } = makeCheckout();
  writeFileSync(join(work, "v2.txt"), "v2\n");
  git(["add", "-A"], work);
  commitAs(work, "v2");
  git(["push", "-q", "origin", "main"], work);
  const target = git(["rev-parse", "HEAD"], work);
  git(["reset", "-q", "--hard", "HEAD~1"], work);
  const skills = join(work, "..", "skills");
  mkdirSync(skills, { recursive: true });
  writeFileSync(join(skills, "stale.install-123"), "staging\n");
  writeFileSync(join(skills, "old.bak"), "staging\n");
  const receipt = join(work, "..", "dell.json");
  const first = runApply([
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
  assert.equal(first.code, 0, JSON.stringify(first.json));
  assert.equal(first.json.verdict, "updated");
  for (const name of SKILL_DIRS)
    assert.ok(existsSync(join(skills, name, "runtime-manifest.json")));
  assert.equal(existsSync(join(skills, "stale.install-123")), false);
  assert.equal(existsSync(join(skills, "old.bak")), false);
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
  assert.equal(existsSync(join(skills, "stale.install-123")), false);
  assert.equal(existsSync(join(skills, "old.bak")), false);
});
