// Late movement gate + preservation (issue #4): branch motion after the
// early check is caught by the signed tip probe before push; the source
// copy, a sibling worktree, the key, and sealed queue slots stay intact.
import { deepStrictEqual, equal, ok } from "node:assert";
import { execFile } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { promisify } from "node:util";
import {
  BRIDGE,
  PYTHON,
  advanceOrigin,
  cli,
  commitFile,
  git,
  makeWorld,
  readJson,
  scriptPath,
  serveFixture,
  submitJob,
} from "./helpers.mjs";

const NODE = process.execPath;

function importBase(w, expectFail = false) {
  const reqCmd = join(w.root, "base-cmd.txt");
  const req = JSON.parse(
    cli(scriptPath("request-base.mjs"), [
      "--shared",
      w.share,
      "--token",
      "e".repeat(16),
      "--lenovo-skill-dir",
      w.root,
      "--node",
      NODE,
      "--lenovo-checkout",
      w.lenovo,
      "--ref-full",
      "refs/heads/main",
      "--request-name",
      "req-e.json",
      "--command-file",
      reqCmd,
      "--repo",
      w.dellHome,
    ]).out
  );
  const jobId = submitJob({
    queue: w.queue,
    keyFile: w.keyFile,
    commandFile: reqCmd,
    cwd: w.lenovo,
  });
  serveFixture({
    queue: w.queue,
    keyFile: w.keyFile,
    jobId,
    allow: {
      "lenovo-base-export.mjs": [
        scriptPath("lenovo-base-export.mjs"),
        ["--request", req.requestPath, "--request-sha256", req.requestSha256],
      ],
    },
  });
  const receipt = join(w.root, "receipt.json");
  const r = cli(scriptPath("import-base.mjs"), [
    "--bridge",
    BRIDGE,
    "--python",
    PYTHON,
    "--queue",
    w.queue,
    "--key-file",
    w.keyFile,
    "--job-id",
    jobId,
    "--shared",
    w.share,
    "--repo",
    w.dellHome,
    "--repo-out",
    join(w.root, "source-copy"),
    "--branch",
    "main",
    "--receipt",
    receipt,
  ]);
  equal(r.code, expectFail ? 1 : 0, r.out);
  return receipt;
}

function probe(w, receipt, token, name) {
  const cmdFile = join(w.root, `${name}-cmd.txt`);
  const req = JSON.parse(
    cli(scriptPath("request-tip.mjs"), [
      "--shared",
      w.share,
      "--token",
      token,
      "--lenovo-skill-dir",
      w.root,
      "--node",
      NODE,
      "--lenovo-checkout",
      w.lenovo,
      "--ref-full",
      "refs/heads/main",
      "--request-name",
      `${name}.json`,
      "--command-file",
      cmdFile,
    ]).out
  );
  const jobId = submitJob({
    queue: w.queue,
    keyFile: w.keyFile,
    commandFile: cmdFile,
    cwd: w.lenovo,
  });
  serveFixture({
    queue: w.queue,
    keyFile: w.keyFile,
    jobId,
    allow: {
      "lenovo-tip-read.mjs": [
        scriptPath("lenovo-tip-read.mjs"),
        ["--request", req.requestPath, "--request-sha256", req.requestSha256],
      ],
    },
  });
  return cli(scriptPath("preflight.mjs"), [
    "--bridge",
    BRIDGE,
    "--python",
    PYTHON,
    "--queue",
    w.queue,
    "--key-file",
    w.keyFile,
    "--job-id",
    jobId,
    "--receipt",
    receipt,
    "--protect",
    w.dellHome,
  ]);
}

test("stale Lenovo checkout still yields the actual remote tip (F1)", () => {
  const w = makeWorld();
  const lenovoHeadBefore = git(["rev-parse", "HEAD"], w.lenovo);
  const fetchHeadPath = join(w.lenovo, ".git", "FETCH_HEAD");
  const fetchHeadBefore = existsSync(fetchHeadPath)
    ? readFileSync(fetchHeadPath)
    : null;
  const refsBefore = git(["for-each-ref"], w.lenovo);
  // Move the remote through a second clone: the Lenovo checkout's local
  // ref goes stale and is never updated by the test.
  const freshTip = advanceOrigin(w, "remote.txt", "v2\n", "remote move");
  const staleLocal = git(["rev-parse", "refs/heads/main"], w.lenovo);
  ok(staleLocal !== freshTip, "fixture is not stale");

  const receipt = importBase(w);
  equal(readJson(receipt).baseSha, freshTip);

  // The owner's checkout is untouched: no branch moved, HEAD kept, tree
  // clean, no new refs, and not even FETCH_HEAD rewritten.
  equal(git(["rev-parse", "refs/heads/main"], w.lenovo), staleLocal);
  equal(git(["rev-parse", "HEAD"], w.lenovo), lenovoHeadBefore);
  equal(git(["status", "--porcelain"], w.lenovo), "");
  equal(git(["for-each-ref"], w.lenovo), refsBefore);
  deepStrictEqual(
    existsSync(fetchHeadPath) ? readFileSync(fetchHeadPath) : null,
    fetchHeadBefore
  );
});

test("remote movement after the early check is caught by the probe (F1)", () => {
  const w = makeWorld();
  const receipt = importBase(w);
  const early = readJson(receipt).baseSha;

  const movedTip = advanceOrigin(w, "late.txt", "moved\n", "late move");
  ok(movedTip !== early);

  const gate = probe(w, receipt, "2".repeat(16), "tip-remote-moved");
  equal(gate.code, 2, gate.out);
  const body = JSON.parse(
    gate.out.split("\n").find((l) => l.trimStart().startsWith("{"))
  );
  equal(body.verifiedSha, early);
  equal(body.currentSha, movedTip);
  equal(body.fresh, false);
});

test("concurrent probes share no mutable state (no FETCH_HEAD race)", async () => {
  const w = makeWorld();
  const fetchHeadPath = join(w.lenovo, ".git", "FETCH_HEAD");
  const fetchHeadBefore = existsSync(fetchHeadPath)
    ? readFileSync(fetchHeadPath)
    : null;
  const refsBefore = git(["for-each-ref"], w.lenovo);
  const runTip = (token, name) => {
    const cmdFile = join(w.root, `${name}-cmd.txt`);
    const req = JSON.parse(
      cli(scriptPath("request-tip.mjs"), [
        "--shared",
        w.share,
        "--token",
        token,
        "--lenovo-skill-dir",
        w.root,
        "--node",
        NODE,
        "--lenovo-checkout",
        w.lenovo,
        "--ref-full",
        "refs/heads/main",
        "--request-name",
        `${name}.json`,
        "--command-file",
        cmdFile,
      ]).out
    );
    return promisify(execFile)("node", [
      scriptPath("lenovo-tip-read.mjs"),
      "--request",
      req.requestPath,
      "--request-sha256",
      req.requestSha256,
    ]);
  };
  // Both drivers run at the same time against the same checkout: with a
  // shared FETCH_HEAD one of them could read the other's fetch.
  const [a, b] = await Promise.all([
    runTip("3".repeat(16), "tip-race-a"),
    runTip("4".repeat(16), "tip-race-b"),
  ]);
  const tipA = JSON.parse(a.stdout).tipSha;
  const tipB = JSON.parse(b.stdout).tipSha;
  ok(/^[0-9a-f]{40}$/.test(tipA), a.stderr);
  equal(tipA, tipB);
  equal(tipA, git(["rev-parse", "refs/heads/main"], w.lenovo).toLowerCase());
  deepStrictEqual(
    existsSync(fetchHeadPath) ? readFileSync(fetchHeadPath) : null,
    fetchHeadBefore
  );
  equal(git(["for-each-ref"], w.lenovo), refsBefore);
  equal(git(["status", "--porcelain"], w.lenovo), "");
});

test("unreachable remote refuses without importing (F1)", () => {
  const w = makeWorld();
  git(["remote", "set-url", "origin", join(w.root, "no-such.git")], w.lenovo);
  const receipt = importBase(w, true);
  const rec = readJson(receipt);
  equal(rec.verdict, "rejected");
  equal(rec.reason, "base-job-failed");
  equal(existsSync(join(w.root, "source-copy")), false);
});

test("movement after the early check is detected by the signed probe before push", () => {
  const w = makeWorld();
  const receipt = importBase(w);
  const early = readJson(receipt).baseSha;

  const fresh = probe(w, receipt, "f".repeat(16), "tip-fresh");
  equal(fresh.code, 0, fresh.out);

  commitFile(w.lenovo, "late.txt", "moved\n", "late move");
  git(["push", "origin", "main"], w.lenovo);

  const gate = probe(w, receipt, "0".repeat(15) + "1", "tip-stale");
  equal(gate.code, 2, gate.out);
  const body = JSON.parse(
    gate.out.split("\n").find((l) => l.trimStart().startsWith("{"))
  );
  equal(body.verifiedSha, early);
  ok(body.currentSha !== early && body.fresh === false);
});

test("flow preserves the copy, sibling worktree, key, and sealed slots", () => {
  const w = makeWorld();
  const keyBefore = readFileSync(w.keyFile);
  const receipt = importBase(w);
  const repoOut = join(w.root, "source-copy");
  const copyBefore = git(["rev-parse", "HEAD"], repoOut);

  const sibling = join(w.root, "sibling-wt");
  git(["worktree", "add", "--detach", sibling, "HEAD"], repoOut);
  const siblingBefore = git(["rev-parse", "HEAD"], sibling);
  const jobSlotBefore = readFileSync(
    join(w.queue, "jobs", `${readJson(receipt).jobId}.json`)
  );

  const fresh = probe(w, receipt, "1".repeat(16), "tip-keep");
  equal(fresh.code, 0, fresh.out);

  equal(git(["rev-parse", "HEAD"], repoOut), copyBefore);
  equal(git(["rev-parse", "HEAD"], sibling), siblingBefore);
  deepStrictEqual(readFileSync(w.keyFile), keyBefore);
  deepStrictEqual(
    readFileSync(join(w.queue, "jobs", `${readJson(receipt).jobId}.json`)),
    jobSlotBefore
  );
});
