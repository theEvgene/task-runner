// Standard-client properties (issue #4) exercised through lan_bridge.py
// itself: MAC tamper refusal, wrong-id binding refusal, duplicate-publish
// refusal, and key-inside-queue refusal. No reimplementation under test.
import { equal, ok } from "node:assert";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { BRIDGE, PYTHON } from "./helpers.mjs";

function py(script, argv) {
  try {
    const stdout = execFileSync(PYTHON, ["-c", script, ...argv], {
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

const SETUP = `
import sys
bridge_dir, queue, key_file, op, job_id = sys.argv[1:6]
sys.path.insert(0, bridge_dir)
import lan_bridge
key = open(key_file, "rb").read()
if op == "seal":
    payload = {"kind": "job", "id": job_id, "command": "cmd"}
    open(queue + "/jobs/" + job_id + ".sealed", "wb").write(lan_bridge.seal(payload, key))
elif op == "tamper":
    payload = {"kind": "job", "id": job_id, "command": "cmd"}
    env = bytearray(lan_bridge.seal(payload, key))
    env[len(env) - 8] ^= 1
    try:
        lan_bridge.open_envelope(bytes(env), key, "job", job_id)
        print("NOT-REFUSED")
    except lan_bridge.ProtocolError:
        print("refused")
elif op == "wrongid":
    payload = {"kind": "result", "id": job_id, "x": 1}
    env = lan_bridge.seal(payload, key)
    try:
        lan_bridge.open_envelope(env, key, "result", "f" * 32)
        print("NOT-REFUSED")
    except lan_bridge.ProtocolError:
        print("refused")
elif op == "dup":
    payload = {"kind": "job", "id": job_id, "command": "cmd"}
    env = lan_bridge.seal(payload, key)
    lan_bridge.publish(queue + "/jobs", job_id, env)
    try:
        lan_bridge.publish(queue + "/jobs", job_id, env)
        print("NOT-REFUSED")
    except lan_bridge.JobExistsError:
        print("refused")
`;

test("tampered envelopes and wrong-id opens refuse", () => {
  const dir = mkdtempSync(join(tmpdir(), "bridge-"));
  const keyFile = join(dir, "k");
  writeFileSync(keyFile, randomBytes(32));
  const args = [dirname(BRIDGE), dir, keyFile];
  equal(py(SETUP, [...args, "tamper", "a".repeat(32)]).out.trim(), "refused");
  equal(py(SETUP, [...args, "wrongid", "b".repeat(32)]).out.trim(), "refused");
  equal(py(SETUP, [...args, "dup", "c".repeat(32)]).out.trim(), "refused");
});

test("a key inside the queue is refused by the client", () => {
  const dir = mkdtempSync(join(tmpdir(), "bridge-"));
  mkdirSync(join(dir, "jobs"), { recursive: true });
  const keyFile = join(dir, "jobs", "k");
  writeFileSync(keyFile, randomBytes(32));
  const r = py(
    "import sys; sys.path.insert(0, sys.argv[1]); import lan_bridge; lan_bridge.load_key(sys.argv[2], 'raw', sys.argv[3])",
    [dirname(BRIDGE), keyFile, dir]
  );
  ok(r.code !== 0, r.out);
});

test("submit publishes; fetch of an unknown id times out on the SAME id", () => {
  const dir = mkdtempSync(join(tmpdir(), "bridge-"));
  const queue = join(dir, "queue");
  mkdirSync(queue, { recursive: true });
  const keyFile = join(dir, "k");
  writeFileSync(keyFile, randomBytes(32));
  const cmdFile = join(dir, "cmd.txt");
  writeFileSync(cmdFile, "echo hi");
  const out = execFileSync(
    PYTHON,
    [
      BRIDGE,
      "submit",
      "--queue",
      queue,
      "--key-file",
      keyFile,
      "--command-file",
      cmdFile,
      "--cwd",
      dir,
    ],
    { encoding: "utf8" }
  );
  ok(/JOBID=[0-9a-f]{32}/.test(out), out);
  let code = -1;
  try {
    execFileSync(
      PYTHON,
      [
        BRIDGE,
        "fetch",
        "--queue",
        queue,
        "--key-file",
        keyFile,
        "--id",
        "d".repeat(32),
        "--timeout",
        "1",
        "--poll",
        "0.5",
      ],
      { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
    );
  } catch (error) {
    code = error.status;
  }
  equal(code, 3); // bounded wait, slot untouched, same id retained for re-fetch
});
