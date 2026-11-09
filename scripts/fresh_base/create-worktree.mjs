#!/usr/bin/env node
// Dell side: cut one dedicated detached task worktree exactly at the verified
// SHA from an import receipt. Verifies HEAD == base afterwards.
// Never checks out a shared branch (always --detach) and refuses to reuse an
// existing path, so the source copy and other worktrees are left alone.
// The input receipt is never modified: the worktree binding is written to
// the explicit --receipt-out path. Exit 0 on success; 1 on any problem
// (fail closed, no guessed base).
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { SHA_RE, fail, parseArgs, shortSha, runGit } from "./lib.mjs";

let opts;
try {
  opts = parseArgs(process.argv.slice(2), {
    "--repo": "required",
    "--receipt": "required",
    "--worktree": "required",
    "--receipt-out": "required",
  });
} catch (error) {
  fail(`usage: ${error.message}`);
  process.exit(2);
}

let receipt;
try {
  receipt = JSON.parse(readFileSync(opts["--receipt"], "utf8"));
} catch (error) {
  fail(`cannot read receipt: ${error.message}`);
  process.exit(1);
}

const sha = receipt?.baseSha ?? receipt?.verifiedSha;
if (!SHA_RE.test(sha ?? "")) {
  fail("receipt has no verified SHA; refusing to guess a base");
  process.exit(1);
}
if (receipt?.verdict && receipt.verdict !== "imported") {
  fail(`receipt verdict is ${receipt.verdict}, not imported`);
  process.exit(1);
}
if (existsSync(opts["--worktree"])) {
  fail(`worktree path already exists: ${opts["--worktree"]}`);
  process.exit(1);
}

const added = await runGit(
  ["worktree", "add", "--detach", opts["--worktree"], sha],
  opts["--repo"]
);
if (!added.ok) {
  fail(`git worktree add failed: ${added.stderr.trim()}`);
  process.exit(1);
}

const head = await runGit(["rev-parse", "HEAD"], opts["--worktree"]);
if (
  !head.ok ||
  head.stdout.trim().toLowerCase() !== String(sha).toLowerCase()
) {
  fail(
    `worktree HEAD ${head.stdout.trim() || "(unknown)"} != verified ${shortSha(sha)}; removing`
  );
  await runGit(
    ["worktree", "remove", "--force", opts["--worktree"]],
    opts["--repo"]
  );
  process.exit(1);
}

const updated = {
  ...receipt,
  worktree: opts["--worktree"],
  worktreeHead: head.stdout.trim(),
};
try {
  writeFileSync(opts["--receipt-out"], `${JSON.stringify(updated, null, 2)}\n`);
} catch (error) {
  fail(`worktree created but receipt write failed: ${error.message}`);
  process.exit(1);
}
console.log(
  JSON.stringify({ worktree: updated.worktree, head: updated.worktreeHead })
);
