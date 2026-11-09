// Local fixtures for fresh-base tests. File paths and file:// git only:
// no network, no Lenovo host, no corporate remote from Dell. Every queue
// operation goes through the existing standard client (lan_bridge.py): jobs
// are sealed by `submit`, results are sealed with its library, and Dell
// scripts consume them with its `fetch` (MAC + kind/id binding verified
// there). The fixture "Lenovo worker" below only transports bytes; trust
// decisions happen inside the client.
import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

export const PYTHON =
  process.env.FRESH_BASE_PYTHON ??
  "C:\\Users\\rb199787\\AppData\\Local\\Programs\\Python\\Python313\\python.exe";
export const BRIDGE =
  process.env.FRESH_BASE_BRIDGE ??
  "C:\\Users\\rb199787\\Documents\\repositories\\personal-agent-toolkit-installed\\skills\\t-bank\\lan-task-bridge\\scripts\\lan_bridge.py";

export function git(args, cwd) {
  return execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
}

export function commitFile(repo, name, content, message) {
  writeFileSync(join(repo, name), content);
  git(["add", name], repo);
  git(
    ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", message],
    repo
  );
}

export function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

// Builds: bare origin, a fixture Lenovo checkout (the only reader of the
// remote), an empty Dell home (Dell never clones the remote), a queue dir, a
// 32-byte key outside all roots, and a LAN share dir.
export function makeWorld() {
  const root = mkdtempSync(join(tmpdir(), "fresh-base-"));
  const remote = join(root, "origin.git");
  git(["init", "--bare", "--initial-branch=main", remote], root);
  const lenovo = join(root, "lenovo");
  git(["clone", remote, lenovo], root);
  commitFile(lenovo, "base.txt", "v1\n", "base");
  git(["push", "origin", "main"], lenovo);
  const dellHome = join(root, "dell");
  const queue = join(root, "queue");
  const share = join(root, "share");
  mkdirSync(join(queue, "jobs"), { recursive: true });
  mkdirSync(join(queue, "results"), { recursive: true });
  mkdirSync(share, { recursive: true });
  mkdirSync(dellHome, { recursive: true });
  const keyFile = join(root, "keys", "queue.key");
  mkdirSync(join(root, "keys"), { recursive: true });
  writeFileSync(keyFile, randomBytes(32));
  return { root, remote, lenovo, dellHome, queue, share, keyFile };
}

export function scriptPath(name) {
  return join(
    dirname(new URL(import.meta.url).pathname.slice(1)),
    "..",
    "..",
    "scripts",
    "fresh_base",
    name
  );
}

export function cli(script, args) {
  try {
    const stdout = execFileSync("node", [script, ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    return { code: 0, out: stdout };
  } catch (error) {
    return {
      code: error.status ?? 1,
      out: `${error.stdout ?? ""}${error.stderr ?? ""}`,
    };
  }
}

// Seal one command through the actual standard client. Returns the job id.
export function submitJob({
  queue,
  keyFile,
  commandFile,
  cwd,
  timeout = 300,
  maxOutput = 65536,
  returnFiles = [],
}) {
  const args = [
    BRIDGE,
    "submit",
    "--queue",
    queue,
    "--key-file",
    keyFile,
    "--command-file",
    commandFile,
    "--cwd",
    cwd,
    "--timeout",
    String(timeout),
    "--max-output",
    String(maxOutput),
  ];
  for (const f of returnFiles) args.push("--return-file", f);
  const out = execFileSync(PYTHON, args, {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  const m = out.match(/JOBID=([0-9a-f]{32})/);
  if (!m) throw new Error(`submit printed no JOBID: ${out}`);
  return m[1];
}

const SHOW_JOB = `
import json, sys
bridge_dir, queue, key_file, job_id = sys.argv[1:5]
sys.path.insert(0, bridge_dir)
import lan_bridge
key = lan_bridge.load_key(key_file, "raw", queue)
raw = lan_bridge.read(queue + "/jobs", job_id)
print(json.dumps(lan_bridge.open_envelope(raw, key, "job", job_id)))
`;

const SEAL_RESULT = `
import sys
bridge_dir, queue, key_file, job_id, exit_code, stdout_file = sys.argv[1:7]
sys.path.insert(0, bridge_dir)
import lan_bridge
key = lan_bridge.load_key(key_file, "raw", queue)
with open(stdout_file, encoding="utf-8") as fh:
    stdout = fh.read()
payload = lan_bridge.build_result_payload(job_id, "ok" if int(exit_code) == 0 else "error",
    int(exit_code), stdout, "")
lan_bridge.publish(queue + "/results", job_id, lan_bridge.seal(payload, key))
`;

// Fixture Lenovo worker: opens the sealed job with the actual client,
// executes the documented driver command it carries, and seals the result
// with the actual client. Unknown commands are never executed.
export function serveFixture({ queue, keyFile, jobId, allow }) {
  const job = JSON.parse(
    execFileSync(
      PYTHON,
      ["-c", SHOW_JOB, dirname(BRIDGE), queue, keyFile, jobId],
      { encoding: "utf8" }
    )
  );
  const command = String(job.command ?? "");
  const entry = Object.entries(allow).find(([marker]) =>
    command.includes(marker)
  );
  if (!entry)
    throw new Error(
      `fixture worker refuses unknown command: ${command.slice(0, 120)}`
    );
  const [, [script, scriptArgs]] = entry;
  let exitCode = 0;
  let stdout = "";
  try {
    stdout = execFileSync("node", [script, ...scriptArgs], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    exitCode = error.status ?? 1;
    stdout = String(error.stdout ?? "");
  }
  const outFile = join(dirname(queue), `stdout-${jobId}.txt`);
  writeFileSync(outFile, stdout);
  execFileSync(PYTHON, [
    "-c",
    SEAL_RESULT,
    dirname(BRIDGE),
    queue,
    keyFile,
    jobId,
    String(exitCode),
    outFile,
  ]);
  return { exitCode, stdout };
}

export { existsSync };
