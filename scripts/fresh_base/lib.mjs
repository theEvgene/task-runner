#!/usr/bin/env node
// Shared helpers for the fresh-base flow (issue #4). No network, no remote
// access from Dell: the only Git operations here run inside local checkouts,
// bundles, and worktrees. Trust always arrives inside a MAC-sealed queue
// result verified by queue.mjs before use.
import { execFile } from "node:child_process";

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

export const BASE_REQUEST_SCHEMA = "fresh-base-request/1";
export const BASE_EXPORT_SCHEMA = "fresh-base-export/1";
export const TIP_REQUEST_SCHEMA = "fresh-base-tip-request/1";
export const TIP_READ_SCHEMA = "fresh-base-tip-read/1";
