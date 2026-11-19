import assert from "node:assert/strict";
import test from "node:test";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

const CHECKER = join(process.cwd(), "scripts", "check-file-tokens.mjs");
const OVER_TOKENS = 5750;
const OVER_BYTES = OVER_TOKENS * 4; // floor(bytes / 4) tokens
const GROWN_BYTES = 6250 * 4;

function git(cwd, ...args) {
  return execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  }).trim();
}

function initRepo() {
  const dir = mkdtempSync(join(tmpdir(), "tokenguard-diff-"));
  git(dir, "init", "-q");
  git(dir, "config", "user.email", "guard-test@example.com");
  git(dir, "config", "user.name", "guard-test");
  git(dir, "config", "commit.gpgsign", "false");
  git(dir, "config", "core.autocrlf", "false");
  return dir;
}

function write(cwd, name, content) {
  const target = join(cwd, name);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function writeBinary(cwd, name, size) {
  const buffer = Buffer.alloc(size, 0x41);
  buffer[511] = 0;
  write(cwd, name, buffer);
}

function commitAll(cwd, message) {
  git(cwd, "add", "-A");
  git(cwd, "commit", "-qm", message);
  return git(cwd, "rev-parse", "HEAD");
}

function text(bytes, char = "x") {
  return char.repeat(bytes);
}

function runDiff(cwd, base, head, extraArgs = []) {
  const result = spawnSync(
    process.execPath,
    [CHECKER, "--base", base, "--head", head, "--cwd", cwd, ...extraArgs],
    { encoding: "utf8" }
  );
  assert.equal(result.error, undefined);
  return {
    status: result.status,
    stdout: result.stdout,
    stderr: result.stderr,
  };
}

test("new over-limit file in range fails and names the file", () => {
  const repo = initRepo();
  write(repo, "small.txt", "hi");
  const base = commitAll(repo, "base");
  write(repo, "docs/big.txt", text(OVER_BYTES));
  const head = commitAll(repo, "add big file");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 1);
  assert.match(result.stdout, /docs\/big\.txt/);
  assert.match(result.stdout, /new in this range/);
  assert.doesNotMatch(result.stdout, /NOTE/);
});

test("file grown over the limit across several commits fails by final content", () => {
  const repo = initRepo();
  write(repo, "big.txt", text(100));
  const base = commitAll(repo, "base");
  write(repo, "big.txt", text(20_000));
  commitAll(repo, "still under");
  write(repo, "big.txt", text(OVER_BYTES));
  const head = commitAll(repo, "over the limit");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 1);
  assert.match(result.stdout, /big\.txt/);
  assert.match(result.stdout, /grown from/);
});

test("intermediate violation fixed before head passes", () => {
  const repo = initRepo();
  write(repo, "big.txt", text(100));
  const base = commitAll(repo, "base");
  write(repo, "big.txt", text(OVER_BYTES));
  commitAll(repo, "temporarily over");
  write(repo, "big.txt", text(100));
  const head = commitAll(repo, "fixed again");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, "");
});

test("pre-existing over-limit file without growth is reported separately and passes", () => {
  const repo = initRepo();
  write(repo, "big.txt", text(OVER_BYTES));
  write(repo, "other.txt", "v1");
  const base = commitAll(repo, "base");
  write(repo, "big.txt", text(OVER_BYTES, "y"));
  write(repo, "other.txt", "v2");
  const head = commitAll(repo, "touch without growth");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 0);
  assert.match(result.stdout, /NOTE/);
  assert.match(result.stdout, /big\.txt/);
  assert.doesNotMatch(result.stdout, /ERROR/);
});

test("grown pre-existing over-limit file fails", () => {
  const repo = initRepo();
  write(repo, "big.txt", text(OVER_BYTES));
  const base = commitAll(repo, "base");
  write(repo, "big.txt", text(GROWN_BYTES));
  const head = commitAll(repo, "grow");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 1);
  assert.match(result.stdout, /grown from/);
});

test("deleted over-limit file does not fail", () => {
  const repo = initRepo();
  write(repo, "big.txt", text(OVER_BYTES));
  const base = commitAll(repo, "base");
  git(repo, "rm", "-q", "big.txt");
  write(repo, "small.txt", "hi");
  const head = commitAll(repo, "delete big file");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, "");
});

test("rename without growth is baseline, rename with growth fails", () => {
  const renamed = initRepo();
  write(renamed, "old.txt", text(OVER_BYTES));
  const renamedBase = commitAll(renamed, "base");
  git(renamed, "mv", "old.txt", "new.txt");
  const renamedHead = commitAll(renamed, "pure rename");
  const pure = runDiff(renamed, renamedBase, renamedHead);
  assert.equal(pure.status, 0);
  assert.match(pure.stdout, /new\.txt/);
  assert.match(pure.stdout, /NOTE/);

  const grown = initRepo();
  write(grown, "old.txt", text(OVER_BYTES));
  const grownBase = commitAll(grown, "base");
  write(grown, "old.txt", text(OVER_BYTES));
  git(grown, "mv", "old.txt", "new.txt");
  const appended = `${text(OVER_BYTES)}\n${text(1000)}`;
  write(grown, "new.txt", appended);
  const grownHead = commitAll(grown, "rename and grow");
  const grownResult = runDiff(grown, grownBase, grownHead);
  assert.equal(grownResult.status, 1);
  assert.match(grownResult.stdout, /new\.txt/);
  assert.match(grownResult.stdout, /grown from/);
});

test("path with spaces over the limit fails and is named", () => {
  const repo = initRepo();
  write(repo, "small.txt", "hi");
  const base = commitAll(repo, "base");
  write(repo, "dir with/sp ace.txt", text(OVER_BYTES));
  const head = commitAll(repo, "add spaced path");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 1);
  assert.match(result.stdout, /sp ace\.txt/);
});

test("binary over the limit is skipped like in staged mode", () => {
  const repo = initRepo();
  write(repo, "small.txt", "hi");
  const base = commitAll(repo, "base");
  writeBinary(repo, "blob.bin", 30_000);
  const head = commitAll(repo, "add binary");
  const result = runDiff(repo, base, head);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, "");
});

test("unknown revision never succeeds", () => {
  const repo = initRepo();
  write(repo, "small.txt", "hi");
  const head = commitAll(repo, "base");
  const result = runDiff(repo, "0".repeat(40), head);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /cannot resolve the merge base/);
});

test("call-site excludes still apply in diff mode", () => {
  const repo = initRepo();
  write(repo, "small.txt", "hi");
  const base = commitAll(repo, "base");
  write(repo, "generated/big.txt", text(OVER_BYTES));
  const head = commitAll(repo, "add generated file");
  const result = runDiff(repo, base, head, ["--exclude", "generated/*"]);
  assert.equal(result.status, 0);
  assert.equal(result.stdout, "");
});

test("staged mode contract is preserved", () => {
  const repo = initRepo();
  write(repo, "small.txt", "hi");
  commitAll(repo, "base");
  write(repo, "staged-big.txt", text(OVER_BYTES));
  git(repo, "add", "staged-big.txt");
  const result = spawnSync(process.execPath, [CHECKER, "--cwd", repo], {
    encoding: "utf8",
  });
  assert.equal(result.status, 1);
  assert.match(result.stdout, /staged-big\.txt/);
});
