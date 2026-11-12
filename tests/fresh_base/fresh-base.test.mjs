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
  git,
  makeWorld,
  readJson,
  scriptPath,
  sealCustomResult,
  serveFixture,
  submitJob,
} from "./helpers.mjs";

const NODE = process.execPath;
const baseArgs = (w) => ["--bridge", BRIDGE, "--python", PYTHON];

// One Dell-side base request (files only). Returns the parsed request JSON
// plus the command file the sealed job must carry.
function requestExport(w, token, name, refFull = "refs/heads/main") {
  const reqCmd = join(w.root, `${name}-cmd.txt`);
  const req = JSON.parse(
    cli(scriptPath("request-base.mjs"), [
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
      refFull,
      "--request-name",
      `${name}.json`,
      "--command-file",
      reqCmd,
      "--repo",
      w.dellHome,
    ]).out
  );
  return { req, reqCmd };
}

function submitExport(w, reqCmd, keyFormat = "raw") {
  return submitJob({
    queue: w.queue,
    keyFile: w.keyFile,
    keyFormat,
    commandFile: reqCmd,
    cwd: w.lenovo,
  });
}

function serveExport(w, req, jobId, keyFormat = "raw") {
  serveFixture({
    queue: w.queue,
    keyFile: w.keyFile,
    keyFormat,
    jobId,
    allow: {
      "lenovo-base-export.mjs": [
        scriptPath("lenovo-base-export.mjs"),
        ["--request", req.requestPath, "--request-sha256", req.requestSha256],
      ],
    },
  });
}

function importArgs(w, jobId, repoOut, receipt, extra = []) {
  return [
    ...baseArgs(w),
    "--queue",
    w.queue,
    "--key-file",
    w.keyFile,
    ...(w.keyFormat === "raw" ? [] : ["--key-format", w.keyFormat]),
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
    ...extra,
  ];
}

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
  const outReceipt = join(w.root, "worktree-receipt.json");
  const created = cli(scriptPath("create-worktree.mjs"), [
    "--repo",
    repoOut,
    "--receipt",
    receipt,
    "--worktree",
    taskWt,
    "--receipt-out",
    outReceipt,
  ]);
  equal(created.code, 0, created.out);
  equal(git(["rev-parse", "HEAD"], taskWt), rec.baseSha);
  deepStrictEqual(readJson(outReceipt).worktreeHead, rec.baseSha);
  // The input receipt is never modified: the binding lands in --receipt-out.
  ok(!("worktreeHead" in readJson(receipt)), "input receipt mutated");
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

test("base64 key works end to end through the real client (F3)", () => {
  const w = makeWorld({ keyFormat: "base64" });
  const { req, reqCmd } = requestExport(w, "e".repeat(16), "req-e64");
  const jobId = submitExport(w, reqCmd, "base64");
  serveExport(w, req, jobId, "base64");
  const receipt = join(w.root, "receipt.json");
  const r = cli(
    scriptPath("import-base.mjs"),
    importArgs(w, jobId, join(w.root, "source-copy"), receipt)
  );
  equal(r.code, 0, r.out);
  equal(readJson(receipt).verdict, "imported");
  ok(/^[0-9a-f]{40}$/.test(readJson(receipt).baseSha));
});

test("a sealed result naming another token's artifacts is refused (request binding)", () => {
  const w = makeWorld();
  const { reqCmd } = requestExport(w, "f".repeat(15) + "0", "req-bind");
  const jobId = submitExport(w, reqCmd);
  // Valid MAC, bound id, but artifacts belong to a different token.
  sealCustomResult({
    queue: w.queue,
    keyFile: w.keyFile,
    jobId,
    stdoutText: JSON.stringify({
      schema: "fresh-base-export/1",
      token: "f".repeat(15) + "0",
      refFull: "refs/heads/main",
      tipSha: "0".repeat(40),
      bundleName: "fresh-base-0000000000000000.bundle",
      manifestName: "fresh-base-0000000000000000.manifest.json",
      sha256: "1".repeat(64),
      bytes: 10,
      manifestDigest: "2".repeat(64),
      remotes: {},
    }),
  });
  const receipt = join(w.root, "receipt.json");
  const r = cli(
    scriptPath("import-base.mjs"),
    importArgs(w, jobId, join(w.root, "must-not-exist"), receipt)
  );
  equal(r.code, 1, r.out);
  equal(readJson(receipt).reason, "request-malformed");
  equal(readJson(receipt).verdict, "mismatch");
});

test("receipt inside the copy is refused on any separator spelling (F2)", () => {
  const w = makeWorld();
  const { req, reqCmd } = requestExport(w, "0".repeat(15) + "1", "req-sep");
  const jobId = submitExport(w, reqCmd);
  serveExport(w, req, jobId);
  const repoOut = join(w.root, "source-copy");
  // Forward-slash spelling of an inside path must refuse too.
  const receiptFwd = `${repoOut.replace(/\\/g, "/")}/receipt.json`;
  const r = cli(
    scriptPath("import-base.mjs"),
    importArgs(w, jobId, repoOut, receiptFwd)
  );
  equal(r.code, 2, r.out);
});

test("shared directory inside the checkout is refused on any separator spelling (F2)", () => {
  const w = makeWorld();
  const inside = join(w.dellHome, "share");
  const { mkdirSync } = process.getBuiltinModule("node:fs");
  mkdirSync(inside, { recursive: true });
  const r = cli(scriptPath("request-base.mjs"), [
    "--shared",
    inside.replace(/\//g, "\\"),
    "--token",
    "0".repeat(15) + "2",
    "--lenovo-skill-dir",
    w.root,
    "--node",
    NODE,
    "--lenovo-checkout",
    w.lenovo,
    "--ref-full",
    "refs/heads/main",
    "--request-name",
    "req-inside.json",
    "--command-file",
    join(w.root, "cmd-inside.txt"),
    "--repo",
    w.dellHome,
  ]);
  equal(r.code, 1, r.out);
});

test("PowerShell-unsafe characters in the signed command are refused (F2)", () => {
  const w = makeWorld();
  const { mkdirSync } = process.getBuiltinModule("node:fs");
  const evil = join(w.root, "sh$are");
  mkdirSync(evil, { recursive: true });
  const r = cli(scriptPath("request-base.mjs"), [
    "--shared",
    evil,
    "--token",
    "0".repeat(15) + "3",
    "--lenovo-skill-dir",
    w.root,
    "--node",
    NODE,
    "--lenovo-checkout",
    w.lenovo,
    "--ref-full",
    "refs/heads/main",
    "--request-name",
    "req-evil.json",
    "--command-file",
    join(w.root, "cmd-evil.txt"),
    "--repo",
    w.dellHome,
  ]);
  equal(r.code, 1, r.out);
});

test("create-worktree without --receipt-out is a usage error (F4)", () => {
  const w = makeWorld();
  const { req, reqCmd } = requestExport(w, "0".repeat(15) + "4", "req-out");
  const jobId = submitExport(w, reqCmd);
  serveExport(w, req, jobId);
  const receipt = join(w.root, "receipt.json");
  const repoOut = join(w.root, "source-copy");
  equal(
    cli(scriptPath("import-base.mjs"), importArgs(w, jobId, repoOut, receipt))
      .code,
    0
  );
  const r = cli(scriptPath("create-worktree.mjs"), [
    "--repo",
    repoOut,
    "--receipt",
    receipt,
    "--worktree",
    join(w.root, "task-wt"),
  ]);
  equal(r.code, 2, r.out);
});
