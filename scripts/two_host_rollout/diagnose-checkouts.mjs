#!/usr/bin/env node
// Read-only checkout diagnosis for issue #161. Inspects one or more local
// candidate checkouts plus their skills dirs and selects the operational
// copy for a later authorized rollout. Never mutates: only git rev-parse,
// status --porcelain, merge-base --is-ancestor and a time-bounded
// ls-remote --heads probe run; a hung network surfaces as unreachable
// no checkout, fetch, pull, merge, reset, clean, force or delete, no writes
// inside any checkout or skills dir. Facts are point-in-time: apply-host
// reverifies via its authenticated fetch immediately before any rollout.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  lstatSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import {
  DIAGNOSE_SCHEMA,
  SHA_RE,
  assertNoSecrets,
  classifyCheckout,
  selectCheckout,
} from "./lib.mjs";
import { assessAllSkills } from "./skills-assess.mjs";

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

const SKILL_NAMES = [
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

function usage(message) {
  process.stderr.write(`two-host-rollout diagnose-checkouts: ${message}\n`);
  process.stderr.write(
    "usage: diagnose-checkouts.mjs --checkout <dir> [--checkout <dir> ...] --target-sha <sha> --is-latest true|false --skills-dir <dir> [--skills-dir <dir> ...] [--origin-ref <ref>] [--out <file>]\n"
  );
  process.exit(2);
}

const raw = process.argv.slice(2);
const checkouts = [];
const skillsDirs = [];
const opts = {};
for (let i = 0; i < raw.length; i += 1) {
  const key = raw[i];
  if (key === "--checkout" || key === "--skills-dir") {
    const value = raw[i + 1];
    if (value === undefined || value.startsWith("--"))
      usage(`${key} requires a value`);
    (key === "--checkout" ? checkouts : skillsDirs).push(value);
    i += 1;
  } else if (
    key === "--target-sha" ||
    key === "--is-latest" ||
    key === "--origin-ref" ||
    key === "--out"
  ) {
    const value = raw[i + 1];
    if (value === undefined || value.startsWith("--"))
      usage(`${key} requires a value`);
    opts[key] = value;
    i += 1;
  } else {
    usage(`unknown argument: ${key}`);
  }
}
if (checkouts.length === 0) usage("--checkout is required");
if (!opts["--target-sha"]) usage("--target-sha is required");
if (!opts["--is-latest"]) usage("--is-latest is required");
if (skillsDirs.length === 0) usage("--skills-dir is required");
const target = String(opts["--target-sha"]).toLowerCase();
if (!SHA_RE.test(target)) usage("--target-sha must be a full commit SHA");
const isLatest =
  opts["--is-latest"] === "true"
    ? true
    : opts["--is-latest"] === "false"
      ? false
      : usage("--is-latest must be true|false");
const originRef = opts["--origin-ref"] ?? "refs/remotes/origin/main";
const outPath = opts["--out"] ? resolve(opts["--out"]) : null;
const resolvedCheckouts = checkouts.map((c) => resolve(c));
let resolvedSkills = skillsDirs.map((s) => resolve(s));
if (resolvedSkills.length === 1 && resolvedCheckouts.length > 1)
  resolvedSkills = resolvedCheckouts.map(() => resolvedSkills[0]);
if (resolvedSkills.length !== resolvedCheckouts.length)
  usage("give one --skills-dir or one per --checkout in the same order");
try {
  assertNoSecrets("argv", raw.join(" "));
} catch (error) {
  usage(error.message);
}

function runGit(checkout, args) {
  return execFileSync("git", args, { cwd: checkout, encoding: "utf8" }).trim();
}

// Bounded remote probe: a hung network must surface as unreachable
// (unavailable/unknown), never hang the read-only diagnosis.
const LS_REMOTE_TIMEOUT_MS = 20000;

function probeOrigin(checkout) {
  try {
    execFileSync("git", ["ls-remote", "--heads", "origin"], {
      cwd: checkout,
      encoding: "utf8",
      timeout: LS_REMOTE_TIMEOUT_MS,
    });
    return true;
  } catch {
    return false;
  }
}

function assessSkills(checkout, skillsDir) {
  let fresh = null;
  try {
    fresh = {};
    for (const relative of RUNTIME_FILES)
      fresh[relative] = createHash("sha256")
        .update(readFileSync(join(checkout, relative)))
        .digest("hex");
  } catch {
    return { state: "runtime-incomplete", perSkill: [], fresh: null };
  }
  // Same basis as the executable rollout path (see skills-assess.mjs):
  // proven junctions into this checkout, managed copies with a current
  // manifest and matching payload, missing entries, and foreign/changed
  // installs each report their own state and basis. A foreign/stale
  // install never truncates the rest of the list, so the operator sees
  // all links at once.
  const assessed = assessAllSkills({
    skillsDir,
    checkout,
    fresh,
    names: SKILL_NAMES,
  });
  const perSkill = assessed.perSkill.map((s) => {
    let kind = s.kind;
    if (s.kind === "link") {
      try {
        kind = lstatSync(join(skillsDir, s.name)).isSymbolicLink()
          ? "symlink"
          : "junction";
      } catch {
        kind = "link";
      }
    }
    const state =
      s.state === "junction-current" || s.state === "copy-current"
        ? "current"
        : s.state === "copy-stale"
          ? "stale"
          : s.state === "foreign"
            ? "ownership-conflict"
            : s.state;
    return {
      name: s.name,
      kind,
      linkTarget: s.linkTarget ?? null,
      state,
      detail: s.detail ?? null,
    };
  });
  return {
    state: assessed.state,
    perSkill,
    fresh,
    detail: assessed.detail ?? null,
  };
}

function diagnoseOne(checkout, skillsDir) {
  const entry = {
    checkout,
    skillsDir,
    branch: null,
    detached: true,
    head: null,
    originMain: null,
    originReachable: false,
    clean: null,
    ancestorOk: null,
    skills: null,
  };
  try {
    entry.branch = runGit(checkout, ["symbolic-ref", "--short", "HEAD"]);
    entry.detached = false;
  } catch {
    entry.branch = null;
    entry.detached = true;
  }
  try {
    entry.head = runGit(checkout, [
      "rev-parse",
      "--verify",
      "HEAD",
    ]).toLowerCase();
  } catch {
    entry.head = null;
  }
  try {
    entry.clean = runGit(checkout, ["status", "--porcelain"]) === "";
  } catch {
    entry.clean = null;
  }
  try {
    entry.originMain = runGit(checkout, [
      "rev-parse",
      "--verify",
      originRef,
    ]).toLowerCase();
  } catch {
    entry.originMain = null;
  }
  try {
    entry.originReachable = probeOrigin(checkout);
  } catch {
    entry.originReachable = false;
  }
  if (entry.head && SHA_RE.test(target)) {
    try {
      execFileSync("git", ["merge-base", "--is-ancestor", entry.head, target], {
        cwd: checkout,
      });
      entry.ancestorOk = true;
    } catch {
      entry.ancestorOk = entry.head === target;
    }
  }
  const skills = assessSkills(checkout, skillsDir);
  entry.skills = {
    dir: skillsDir,
    state: skills.state,
    perSkill: skills.perSkill,
    detail: skills.detail ?? null,
  };
  const classification = classifyCheckout(
    {
      branch: entry.detached ? null : entry.branch,
      head: entry.head,
      originMain: entry.originMain,
      originReachable: entry.originReachable,
      clean: entry.clean,
      ancestorOk: entry.ancestorOk,
      skillsState: skills.state,
    },
    { target, isLatest }
  );
  entry.status = classification.status;
  entry.reason = classification.reason;
  entry.nextAction = classification.nextAction;
  return entry;
}

const results = resolvedCheckouts.map((checkout, i) =>
  diagnoseOne(checkout, resolvedSkills[i])
);
const selection = selectCheckout(
  results.map((r) => ({
    checkout: r.checkout,
    classification: {
      status: r.status,
      reason: r.reason,
      nextAction: r.nextAction,
    },
  })),
  { target, isLatest }
);
const report = {
  schema: DIAGNOSE_SCHEMA,
  targetSha: target,
  isLatest,
  originRef,
  checkouts: results,
  selection,
  reverify:
    "point-in-time facts only; apply-host reverifies via its authenticated fetch immediately before any authorized rollout; reset, force and delete are never used",
  nextAction: selection.nextAction,
};
try {
  assertNoSecrets("report", JSON.stringify(report));
} catch (error) {
  process.stderr.write(
    `two-host-rollout diagnose-checkouts: ${error.message}\n`
  );
  process.exit(2);
}
if (outPath) {
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`);
}
console.log(JSON.stringify(report));
process.exit(selection.outcome === "justified-choice" ? 0 : 1);
