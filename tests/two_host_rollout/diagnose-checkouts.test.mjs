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
import {
  classifyCheckout,
  selectCheckout,
} from "../../scripts/two_host_rollout/lib.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const diagnose = join(
  repoRoot,
  "scripts",
  "two_host_rollout",
  "diagnose-checkouts.mjs"
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

function makeCheckout() {
  const root = mkdtempSync(join(tmpdir(), "two-host-diag-"));
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
  git(["add", "-A"], work);
  commitAs(work, "runtime");
  git(["push", "-q", "origin", "main"], work);
  return { root, remote, work };
}

function run(args) {
  try {
    const out = execFileSync("node", [diagnose, ...args], { encoding: "utf8" });
    return { code: 0, json: JSON.parse(out) };
  } catch (error) {
    const text = `${error.stdout ?? ""}`;
    return {
      code: error.status ?? 1,
      json: JSON.parse(text.trim().split("\n").pop()),
    };
  }
}

test("pure: unknown is never ready; dirty/diverged/detached/remote explicit", () => {
  const t = "a".repeat(40);
  const ready = {
    branch: "main",
    head: t,
    originMain: t,
    originReachable: true,
    clean: true,
    ancestorOk: true,
    skillsState: "current",
  };
  assert.equal(
    classifyCheckout(ready, { target: t, isLatest: true }).status,
    "ready"
  );
  assert.equal(
    classifyCheckout({ ...ready, clean: null }, { target: t, isLatest: true })
      .status,
    "unknown"
  );
  assert.equal(
    classifyCheckout({ ...ready, head: "bad" }, { target: t, isLatest: true })
      .status,
    "unknown"
  );
  assert.equal(
    classifyCheckout({ ...ready, clean: false }, { target: t, isLatest: true })
      .reason,
    "dirty-checkout"
  );
  assert.equal(
    classifyCheckout(
      { ...ready, head: "b".repeat(40), ancestorOk: false },
      { target: t, isLatest: true }
    ).reason,
    "diverged"
  );
  assert.equal(
    classifyCheckout({ ...ready, branch: null }, { target: t, isLatest: true })
      .reason,
    "detached"
  );
  assert.equal(
    classifyCheckout(
      { ...ready, originReachable: false },
      { target: t, isLatest: true }
    ).reason,
    "remote-unavailable"
  );
  assert.equal(
    classifyCheckout(
      { ...ready, branch: "feature", head: "b".repeat(40) },
      { target: t, isLatest: true }
    ).reason,
    "wrong-branch"
  );
});

test("pure: selection is deterministic; ambiguity needs explicit rerun", () => {
  const ready = (checkout) => ({
    checkout,
    classification: { status: "ready", reason: "ready-current" },
  });
  const blocked = (checkout) => ({
    checkout,
    classification: {
      status: "not-ready",
      reason: "dirty-checkout",
      nextAction: "fix",
    },
  });
  const one = selectCheckout([blocked("/b"), ready("/a")], {
    target: "a".repeat(40),
    isLatest: true,
  });
  assert.equal(one.outcome, "justified-choice");
  assert.equal(one.choice, "/a");
  const swapped = selectCheckout([ready("/a"), blocked("/b")], {
    target: "a".repeat(40),
    isLatest: true,
  });
  assert.equal(swapped.choice, one.choice);
  const ambiguous = selectCheckout([ready("/b"), ready("/a")], {
    target: "a".repeat(40),
    isLatest: true,
  });
  assert.equal(ambiguous.outcome, "ambiguous");
  assert.equal(ambiguous.choice, null);
  assert.match(ambiguous.nextAction, /single --checkout/);
  const none = selectCheckout([blocked("/b"), blocked("/a")], {
    target: "a".repeat(40),
    isLatest: true,
  });
  assert.equal(none.outcome, "no-ready-candidate");
});

test("cli: clean current checkout reports facts and justified choice", () => {
  const { work } = makeCheckout();
  const target = git(["rev-parse", "HEAD"], work);
  const skills = join(work, "..", "skills");
  const r = run([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
  ]);
  assert.equal(r.code, 0, JSON.stringify(r.json));
  assert.equal(r.json.schema, "two-host-rollout-diagnose/1");
  const entry = r.json.checkouts[0];
  assert.equal(entry.branch, "main");
  assert.equal(entry.head.toLowerCase(), target.toLowerCase());
  assert.equal(entry.clean, true);
  assert.equal(entry.detached, false);
  assert.equal(entry.originReachable, true);
  assert.equal(entry.status, "ready");
  assert.equal(r.json.selection.outcome, "justified-choice");
  assert.equal(r.json.selection.choice, entry.checkout);
  assert.match(r.json.reverify, /reverify|reverif/i);
  assert.match(r.json.nextAction, /reverify|apply-host/i);
});

test("cli: dirty, diverged, detached and unreachable remote stay explicit; nothing mutates", () => {
  const w1 = makeCheckout();
  writeFileSync(join(w1.work, "dirty.txt"), "x\n");
  const head1 = git(["rev-parse", "HEAD"], w1.work);
  const branch1 = git(["symbolic-ref", "--short", "HEAD"], w1.work);
  const r1 = run([
    "--checkout",
    w1.work,
    "--target-sha",
    head1,
    "--is-latest",
    "true",
    "--skills-dir",
    join(w1.work, "..", "s"),
  ]);
  assert.equal(r1.json.checkouts[0].reason, "dirty-checkout");
  assert.equal(git(["rev-parse", "HEAD"], w1.work), head1);
  assert.equal(git(["symbolic-ref", "--short", "HEAD"], w1.work), branch1);

  const w2 = makeCheckout();
  const published = git(["rev-parse", "HEAD"], w2.work);
  writeFileSync(join(w2.work, "local.txt"), "local\n");
  git(["add", "-A"], w2.work);
  commitAs(w2.work, "local");
  const localHead = git(["rev-parse", "HEAD"], w2.work);
  const r2 = run([
    "--checkout",
    w2.work,
    "--target-sha",
    published,
    "--is-latest",
    "true",
    "--skills-dir",
    join(w2.work, "..", "s"),
  ]);
  assert.ok(
    ["diverged", "revision-mismatch"].includes(r2.json.checkouts[0].reason)
  );
  assert.equal(git(["rev-parse", "HEAD"], w2.work), localHead);

  const w3 = makeCheckout();
  const t3 = git(["rev-parse", "HEAD"], w3.work);
  git(["checkout", "-q", "--detach", t3], w3.work);
  const r3 = run([
    "--checkout",
    w3.work,
    "--target-sha",
    t3,
    "--is-latest",
    "true",
    "--skills-dir",
    join(w3.work, "..", "s"),
  ]);
  assert.equal(r3.json.checkouts[0].reason, "detached");
  assert.equal(
    git(["rev-parse", "HEAD"], w3.work).toLowerCase(),
    t3.toLowerCase()
  );

  const w4 = makeCheckout();
  const t4 = git(["rev-parse", "HEAD"], w4.work);
  git(["remote", "remove", "origin"], w4.work);
  const r4 = run([
    "--checkout",
    w4.work,
    "--target-sha",
    t4,
    "--is-latest",
    "true",
    "--skills-dir",
    join(w4.work, "..", "s"),
  ]);
  assert.equal(r4.json.checkouts[0].reason, "remote-unavailable");
  assert.equal(r4.json.checkouts[0].originReachable, false);
  assert.notEqual(r4.json.checkouts[0].status, "ready");
});

test("cli: several candidates give a justified choice or ambiguity; choice is stable", () => {
  const good = makeCheckout();
  const target = git(["rev-parse", "HEAD"], good.work);
  const bad = makeCheckout();
  writeFileSync(join(bad.work, "dirty.txt"), "x\n");
  const skills = join(good.root, "skills");
  const first = run([
    "--checkout",
    bad.work,
    "--checkout",
    good.work,
    "--target-sha",
    target,
    "--is-latest",
    "false",
    "--skills-dir",
    skills,
  ]);
  // Pinned mode ignores the unreachable/other-repo origin: dirty copy is out,
  // clean copy is the single justified choice even though origins differ.
  assert.equal(first.json.selection.outcome, "justified-choice");
  assert.equal(first.json.selection.choice, good.work);
  const second = run([
    "--checkout",
    good.work,
    "--checkout",
    bad.work,
    "--target-sha",
    target,
    "--is-latest",
    "false",
    "--skills-dir",
    skills,
  ]);
  assert.equal(second.json.selection.choice, first.json.selection.choice);

  const other = makeCheckout();
  git(["checkout", "-q", "--detach", target], good.work);
  const ambTarget = git(["rev-parse", "HEAD"], good.work);
  const amb = run([
    "--checkout",
    good.work,
    "--checkout",
    other.work,
    "--target-sha",
    ambTarget,
    "--is-latest",
    "false",
    "--skills-dir",
    skills,
  ]);
  // Pinned mode: both copies are installable (missing skills) so both are
  // ready and the choice is explicitly ambiguous, never silent.
  assert.equal(amb.json.selection.outcome, "ambiguous");
  assert.equal(amb.json.selection.choice, null);
  assert.match(amb.json.selection.nextAction, /single --checkout/);
});

test("cli: foreign skills refuse as ownership-conflict and unknown origin is never current", () => {
  const { work } = makeCheckout();
  const target = git(["rev-parse", "HEAD"], work);
  const skills = join(work, "..", "skills");
  const foreign = join(skills, "cli-assign-jira-issue");
  mkdirSync(foreign, { recursive: true });
  writeFileSync(join(foreign, "foreign.txt"), "not managed\n");
  const r = run([
    "--checkout",
    work,
    "--target-sha",
    target,
    "--is-latest",
    "true",
    "--skills-dir",
    skills,
  ]);
  assert.equal(r.json.checkouts[0].skills.state, "ownership-conflict");
  assert.equal(r.json.checkouts[0].status, "not-ready");
  assert.equal(existsSync(join(foreign, "runtime-manifest.json")), false);
});

test("cli: diagnose script performs no mutation verbs", () => {
  const text = readFileSync(diagnose, "utf8");
  for (const verb of [
    "git checkout",
    "git fetch",
    "git reset",
    "git clean",
    "reset --hard",
    "--force",
    "rmSync",
    "rename(",
  ]) {
    // ls-remote is the only allowed network read; checkout/fetch/reset/clean
    // and any destructive fs mutation must not appear in this file.
    assert.ok(!text.includes(verb), `diagnose must not contain: ${verb}`);
  }
  assert.ok(text.includes("ls-remote"));
  assert.ok(!text.includes("reset --hard"));
});
