#!/usr/bin/env node
// Shared helpers for the fresh-base flow (issue #4). No network, no remote
// access from Dell: the only Git operations here run inside local checkouts,
// bundles, and worktrees. Trust always arrives inside a MAC-sealed queue
// result verified by the existing lan_bridge.py client before use.
import { execFile, execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { tmpdir } from "node:os";

export const SHA_RE = /^[0-9a-f]{40}$|^[0-9a-f]{64}$/i;
export const SHA256_RE = /^[0-9a-f]{64}$/i;
export const JOB_RE = /^[0-9a-f]{32}$/;
export const TOKEN_RE = /^[0-9a-f]{16}$/;
export const BRANCH_RE = /^[A-Za-z0-9._/-]+$/;

export function fail(message) {
  process.stderr.write(`fresh-base: ${message}\n`);
}

export function runGit(args, cwd) {
  return new Promise((resolve) => {
    execFile(
      "git",
      args,
      { cwd, encoding: "utf8" },
      (error, stdout, stderr) => {
        if (error) {
          resolve({
            ok: false,
            code: error.code ?? 1,
            stdout: String(stdout ?? ""),
            stderr: String(stderr ?? error.message),
          });
          return;
        }
        resolve({
          ok: true,
          code: 0,
          stdout: stdout ?? "",
          stderr: stderr ?? "",
        });
      }
    );
  });
}

export function parseArgs(argv, specs) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const spec = specs[arg];
    if (spec === undefined) throw new Error(`unknown argument: ${arg}`);
    if (spec === true) {
      out[arg] = true;
      continue;
    }
    const value = argv[i + 1];
    if (value === undefined) throw new Error(`${arg} requires a value`);
    i += 1;
    if (spec === "repeatable") {
      out[arg] = out[arg] ?? [];
      out[arg].push(value);
      continue;
    }
    out[arg] = value;
  }
  for (const [key, spec] of Object.entries(specs)) {
    if (spec === "required" && out[key] === undefined) {
      throw new Error(`${key} is required`);
    }
  }
  return out;
}

export function safeName(value) {
  if (typeof value !== "string" || value.trim() === "") return false;
  return (
    !value.includes("/") &&
    !value.includes("\\") &&
    !value.includes("..") &&
    !value.includes(":") &&
    !value.startsWith(".")
  );
}

export function shortSha(sha) {
  return String(sha).slice(0, 12);
}

// Canonical identity of a path: resolves symlinks/junctions when the path
// exists, normalizes separators, folds case on Windows.
export function canonicalPath(value) {
  const abs = resolve(value);
  let real = abs;
  try {
    if (existsSync(abs)) real = realpathSync(abs);
  } catch {
    real = abs;
  }
  return process.platform === "win32" ? real.toLowerCase() : real;
}

// True when child is root or lives under it, on any separator spelling.
// Relative() returns an absolute path for different drives (Windows) or
// a ".."-led path for escape: both count as outside.
export function isInside(child, root) {
  const rel = relative(canonicalPath(root), canonicalPath(child));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

// Parts interpolated into the double-quoted signed command must survive
// PowerShell and cmd.exe intact: forbid `"`, `$`, backtick, `%`, and
// control characters (expansion, subexpression, and variable markers).
// Spaces are fine; hex digests and flags never reach this check.
export function assertSafeCommandPart(label, value) {
  if (/["$`%\x00-\x1f\x7f]/.test(value ?? "")) {
    fail(`${label} contains characters unsafe for the signed command`);
    process.exit(1);
  }
}

export const BASE_REQUEST_SCHEMA = "fresh-base-request/1";
export const BASE_EXPORT_SCHEMA = "fresh-base-export/1";
export const TIP_REQUEST_SCHEMA = "fresh-base-tip-request/1";
export const TIP_READ_SCHEMA = "fresh-base-tip-read/1";

// Observe the actual remote tip into an ISOLATED temporary bare repo and
// return its SHA bound to refFull. Nothing is ever written to the shared
// checkout: no FETCH_HEAD, no ref updates, no worktree touch. Each call
// owns its temp repo and dedicated ref (refs/fresh-base/<token>), so
// concurrent jobs cannot race each other. Fail closed on any step.
export function fetchRemoteTip({ originUrl, refFull, token }) {
  const fail = (reason, detail = "") => ({ ok: false, reason, detail });
  if (!/^[0-9a-f]{16}$/.test(token ?? "")) return fail("bad token");
  let observed;
  try {
    observed = execFileSync("git", ["ls-remote", originUrl, refFull], {
      encoding: "utf8",
    }).trim();
  } catch (error) {
    return fail("remote-unavailable", String(error.message).slice(0, 200));
  }
  const lines = observed
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length !== 1) return fail("remote-unavailable", "ambiguous ref");
  const [sha, gotRef] = lines[0].split(/\s+/);
  if (gotRef !== refFull || !SHA_RE.test(sha ?? "")) {
    return fail("remote-unavailable", "no factual tip");
  }
  const tip = sha.toLowerCase();
  const workdir = mkdtempSync(join(tmpdir(), "fresh-base-fetch-"));
  try {
    execFileSync("git", ["init", "--bare", "-q"], { cwd: workdir });
    execFileSync(
      "git",
      [
        "fetch",
        "--no-write-fetch-head",
        "--quiet",
        originUrl,
        `${refFull}:refs/fresh-base/${token}`,
      ],
      { cwd: workdir }
    );
    const fetched = execFileSync(
      "git",
      ["rev-parse", "--verify", `refs/fresh-base/${token}^{commit}`],
      { cwd: workdir, encoding: "utf8" }
    )
      .trim()
      .toLowerCase();
    if (fetched !== tip) {
      removeDir(workdir);
      return fail("bundle-tip-mismatch", "fetch drifted");
    }
    // Bundle callers must name this ref (not the bare SHA): git bundle
    // refuses a SHA-only revision as an empty bundle.
    const ref = `refs/fresh-base/${token}`;
    return { ok: true, sha: tip, repoDir: workdir, ref };
  } catch (error) {
    removeDir(workdir);
    return fail("remote-unavailable", String(error.message).slice(0, 200));
  }
}

export function removeDir(dir) {
  try {
    rmSync(dir, { recursive: true, force: true });
  } catch {
    // best effort: temp only, never the checkout
  }
}
