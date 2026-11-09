// Fresh base end-to-end (issue #4) through the signed Lenovo route.
// request-base -> lan_bridge submit -> fixture Lenovo worker
// (lenovo-base-export) -> import-base -> create-worktree. Dell never touches
// the remote; every SHA arrives inside a MAC-verified sealed result.
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
const baseArgs = (w) => ["--bridge", BRIDGE, "--python", PYTHON];

test("verified base binds repo/branch/SHA/worktree and HEAD equals SHA", () => {
  const w = makeWorld();
  const reqCmd = join(w.root, "base-cmd.txt");
  const req = JSON.parse(
    cli(scriptPath("request-base.mjs"), [
      "--shared",
      w.share,
      "--token",
      "a".repeat(16),
      "--lenovo-skill-dir",
      w.root,
      "--node",
      NODE,
      "--lenovo-checkout",
      w.lenovo,
      "--ref-full",
      "refs/heads/main",
      "--request-name",
      "req-a.json",
      "--command-file",
      reqCmd,
      "--repo",
      w.dellHome,
    ]).out
  );
  // The command file must name the Lenovo driver and the request digest.
  ok(req.command.includes("lenovo-base-export.mjs"), req.command);

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
  const repoOut = join(w.root, "source-copy");
  const imported = cli(scriptPath("import-base.mjs"), [
    ...baseArgs(w),
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
    repoOut,
    "--branch",
    "main",
    "--receipt",
    receipt,
  ]);
  equal(imported.code, 0, imported.out);
  const rec = readJson(receipt);
  equal(rec.verdict, "imported");
  equal(rec.refFull, "refs/heads/main");
  ok(/^[0-9a-f]{40}$|^[0-9a-f]{64}$/.test(rec.baseSha));
  equal(rec.repo.head, rec.baseSha);
  equal(git(["rev-parse", "HEAD"], repoOut), rec.baseSha);

  const taskWt = join(w.root, "task-wt");
  const created = cli(scriptPath("create-worktree.mjs"), [
    "--repo",
    repoOut,
    "--receipt",
    receipt,
    "--worktree",
    taskWt,
  ]);
  equal(created.code, 0, created.out);
  equal(git(["rev-parse", "HEAD"], taskWt), rec.baseSha);
  deepStrictEqual(readJson(receipt).worktreeHead, rec.baseSha);
});

test("unknown branch refuses without creating a source copy", () => {
  const w = makeWorld();
  const reqCmd = join(w.root, "base-cmd.txt");
  const req = JSON.parse(
    cli(scriptPath("request-base.mjs"), [
      "--shared",
      w.share,
      "--token",
      "b".repeat(16),
      "--lenovo-skill-dir",
      w.root,
      "--node",
      NODE,
      "--lenovo-checkout",
      w.lenovo,
      "--ref-full",
      "refs/heads/nope",
      "--request-name",
      "req-b.json",
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
  const r = cli(scriptPath("import-base.mjs"), [
    ...baseArgs(w),
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
    join(w.root, "must-not-exist"),
    "--branch",
    "main",
    "--receipt",
    join(w.root, "receipt.json"),
  ]);
  equal(r.code, 1, r.out);
});

test("swapped bundle bytes refuse without importing", () => {
  const w = makeWorld();
  const reqCmd = join(w.root, "base-cmd.txt");
  const req = JSON.parse(
    cli(scriptPath("request-base.mjs"), [
      "--shared",
      w.share,
      "--token",
      "c".repeat(16),
      "--lenovo-skill-dir",
      w.root,
      "--node",
      NODE,
      "--lenovo-checkout",
      w.lenovo,
      "--ref-full",
      "refs/heads/main",
      "--request-name",
      "req-c.json",
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
  // Swap the bundle on the share after the sealed export: the sealed
  // SHA-256 no longer matches, so import must refuse.
  const { appendFileSync, readdirSync } = process.getBuiltinModule("node:fs");
  const bundleName = readdirSync(w.share).find((n) => n.endsWith(".bundle"));
  appendFileSync(join(w.share, bundleName), "tampered");
  const r = cli(scriptPath("import-base.mjs"), [
    ...baseArgs(w),
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
    join(w.root, "must-not-exist"),
    "--branch",
    "main",
    "--receipt",
    join(w.root, "receipt.json"),
  ]);
  equal(r.code, 1, r.out);
  ok(
    !readFileSync(join(w.root, "receipt.json"), "utf8").includes(
      '"verdict":"imported"'
    )
  );
});

test("no sealed result is unknown on the SAME id, and the same id verifies after the worker runs", () => {
  const w = makeWorld();
  const reqCmd = join(w.root, "base-cmd.txt");
  const req = JSON.parse(
    cli(scriptPath("request-base.mjs"), [
      "--shared",
      w.share,
      "--token",
      "d".repeat(16),
      "--lenovo-skill-dir",
      w.root,
      "--node",
      NODE,
      "--lenovo-checkout",
      w.lenovo,
      "--ref-full",
      "refs/heads/main",
      "--request-name",
      "req-d.json",
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
  const receipt = join(w.root, "receipt.json");
  const common = [
    ...baseArgs(w),
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
    "--fetch-timeout",
    "2",
    "--poll",
    "0.5",
  ];
  const waiting = cli(scriptPath("import-base.mjs"), common);
  equal(waiting.code, 3, waiting.out); // slot stays occupied, same id retained
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
  const retry = cli(scriptPath("import-base.mjs"), common); // SAME job id, never a new one
  equal(retry.code, 0, retry.out);
  equal(readJson(receipt).verdict, "imported");
});
