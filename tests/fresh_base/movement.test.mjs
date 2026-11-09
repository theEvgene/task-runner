// Late movement gate + preservation (issue #4): branch motion after the
// early check is caught by the signed tip probe before push; the source
// copy, a sibling worktree, the key, and sealed queue slots stay intact.
import { deepStrictEqual, equal, ok } from "node:assert";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import {
  BRIDGE,
  PYTHON,
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

function importBase(w) {
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
  equal(r.code, 0, r.out);
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
  ]);
}

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
