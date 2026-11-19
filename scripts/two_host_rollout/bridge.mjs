#!/usr/bin/env node
// Thin Node binding to the existing signed transport client
// (scripts/lan_bridge/lan_bridge.py). All sealing, MAC verification,
// kind/id binding, immutable publish, and key-file rules execute inside
// that client; nothing here reimplements them. Key placement is
// additionally proven outside every caller-named root before delegating.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";

export function resolvePython(opts) {
  return (
    opts["--python"] ?? process.env.TWO_HOST_ROLLOUT_PYTHON ?? "python"
  );
}

export function resolveBridge(opts) {
  const bridge = opts["--bridge"] ?? process.env.TWO_HOST_ROLLOUT_BRIDGE;
  if (!bridge) {
    process.stderr.write(
      "two-host-rollout: --bridge <lan_bridge.py> is required (owner-provided, never guessed)\n"
    );
    process.exit(2);
  }
  if (!existsSync(bridge)) {
    process.stderr.write(`two-host-rollout: bridge client not found: ${bridge}\n`);
    process.exit(2);
  }
  return bridge;
}

function canonical(value) {
  const abs = resolve(value);
  return process.platform === "win32" ? abs.toLowerCase() : abs;
}

export function isInside(child, root) {
  const rel = relative(canonical(root), canonical(child));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

// Path placement only (no crypto): the key must not sit where the queue or
// a caller-named receipt root can carry it. The client re-checks its roots.
export function assertKeyOutside(keyFile, roots) {
  for (const root of roots) {
    if (root && isInside(keyFile, root)) {
      process.stderr.write(
        `two-host-rollout: refusing key inside protected root: ${root}\n`
      );
      process.exit(2);
    }
  }
}

export function runBridge({ python, bridge, args, timeoutMs }) {
  const proc = spawnSync(python, [bridge, ...args], {
    encoding: "utf8",
    timeout: timeoutMs,
  });
  if (proc.error) {
    return {
      code: 2,
      stdout: "",
      stderr: `bridge client failed: ${proc.error.message}`,
    };
  }
  return {
    code: proc.status ?? 2,
    stdout: proc.stdout ?? "",
    stderr: proc.stderr ?? "",
  };
}

// Fetch the MAC-verified result through the standard client. Exit 0 carries
// the payload; exit 3 is a bounded-wait timeout: the slot stays occupied and
// the caller re-fetches the SAME id, never a new one.
export function fetchResult({
  python,
  bridge,
  queue,
  keyFile,
  keyFormat,
  id,
  timeout,
  poll,
}) {
  const r = runBridge({
    python,
    bridge,
    args: [
      "fetch",
      "--queue",
      queue,
      "--key-file",
      keyFile,
      "--key-format",
      keyFormat,
      "--id",
      id,
      "--timeout",
      String(timeout),
      "--poll",
      String(poll),
    ],
    timeoutMs: Number(timeout) * 1000 + 120000,
  });
  if (r.code === 0)
    return { code: 0, payload: JSON.parse(r.stdout), stderr: r.stderr };
  return { code: r.code, payload: null, stderr: (r.stderr || r.stdout).trim() };
}

const SHOW_JOB = `
import json, sys
bridge_dir, queue, key_file, key_format, job_id = sys.argv[1:6]
sys.path.insert(0, bridge_dir)
import lan_bridge
key = lan_bridge.load_key(key_file, key_format, queue)
raw = lan_bridge.read(queue + "/jobs", job_id)
job = lan_bridge.open_envelope(raw, key, "job", job_id)
print(json.dumps(job))
`;

// Read the sealed job through the standard client (MAC + kind/id binding).
export function readJob({ python, bridge, queue, keyFile, keyFormat, id }) {
  const proc = spawnSync(
    python,
    ["-c", SHOW_JOB, dirname(bridge), queue, keyFile, keyFormat, id],
    { encoding: "utf8", timeout: 60000 }
  );
  if (proc.status !== 0)
    throw new Error(
      `job unverifiable: ${(proc.stderr || proc.stdout || "").trim()}`
    );
  return JSON.parse(proc.stdout);
}
