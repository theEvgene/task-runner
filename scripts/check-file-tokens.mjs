#!/usr/bin/env node
// Per-file token budget guard. Behavior-preserving port of the checker pinned in
// https://github.com/mattpocock/course-video-manager/blob/d329576deeda2878b2902a2d3d09749bbd1af693/scripts/check-file-tokens.sh
// Estimates tokens as floor(bytes / 4); no tokenizer or network dependency.
import { closeSync, openSync, readFileSync, readSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { TextDecoder } from "node:util";

export const MAX_TOKENS = 5500;
const BYTES_PER_TOKEN = 4;
const BINARY_SAMPLE_BYTES = 64 * 1024;

const USAGE = [
  "Usage: node scripts/check-file-tokens.mjs [options]",
  "",
  "Options:",
  "  --all                  check every tracked file (git ls-files) instead of staged files",
  "  --base <sha> --head <sha>",
  "                         check only files changed between the merge base of",
  "                         <base> and <head> and <head> (a pull request scope)",
  "  --exclude <pattern>    skip files matching a glob; repeatable, * also matches /",
  "  --exclude-from <file>  read extra globs from a file (one per line, # comments)",
  "  --cwd <dir>            run inside another repository checkout",
  "  -h, --help             show this help",
].join("\n");

export function estimateTokens(bytes) {
  return Math.floor(bytes / BYTES_PER_TOKEN);
}

export function toPosixPath(file) {
  return file.replace(/\\/g, "/");
}

// Bash `case` globs: `*` crosses `/`, `?` is one character, `[...]` is a class.
export function globToRegExp(pattern) {
  let source = "";
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    if (char === "*") {
      source += ".*";
    } else if (char === "?") {
      source += ".";
    } else if (char === "[" && pattern.indexOf("]", index + 2) > index) {
      const end = pattern.indexOf("]", index + 2);
      const body = pattern.slice(index + 1, end).replace(/^!/, "^");
      source += `[${body}]`;
      index = end;
    } else {
      source += char.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
  }
  return new RegExp(`^${source}$`);
}

export function isExcluded(file, patterns) {
  const normalized = toPosixPath(file);
  return patterns.some((pattern) => globToRegExp(pattern).test(normalized));
}

function trimIncompleteUtf8Tail(buffer) {
  for (let back = 1; back <= 3 && buffer.length >= back; back += 1) {
    const byte = buffer[buffer.length - back];
    if ((byte & 0xc0) === 0x80) continue;
    const length = byte >= 0xf0 ? 4 : byte >= 0xe0 ? 3 : byte >= 0xc0 ? 2 : 1;
    return length > back ? buffer.subarray(0, buffer.length - back) : buffer;
  }
  return buffer;
}

function isUtf8(buffer) {
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(buffer);
    return true;
  } catch {
    return false;
  }
}

function readSample(path, size) {
  const handle = openSync(path, "r");
  try {
    const buffer = Buffer.alloc(Math.min(size, BINARY_SAMPLE_BYTES));
    const read = buffer.length
      ? readSync(handle, buffer, 0, buffer.length, 0)
      : 0;
    return buffer.subarray(0, read);
  } finally {
    closeSync(handle);
  }
}

// A file is binary when it has a NUL byte or is not valid UTF-8.
export function isBinarySample(buffer) {
  if (buffer.includes(0)) return true;
  return !isUtf8(trimIncompleteUtf8Tail(buffer));
}

const SHA_RE = /^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$/;

export function isCommitSha(value) {
  return typeof value === "string" && SHA_RE.test(value);
}

// Resolve the merge base of a pull request scope. Comparing from the merge
// base instead of from <base> keeps a branch that is behind its target from
// reporting every target-only file as a change. Any failure here is a broken
// scope, never a reason to silently check a different file set.
export function resolveScopeMergeBase({ base, head, cwd = process.cwd() }) {
  if (!isCommitSha(base) || !isCommitSha(head)) {
    throw new Error("--base and --head must each be a full commit SHA");
  }
  let mergeBase;
  try {
    mergeBase = execFileSync("git", ["merge-base", "--", base, head], {
      cwd,
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
    })
      .trim()
      .toLowerCase();
  } catch (error) {
    throw new Error(`cannot resolve the merge base of ${base} and ${head}`);
  }
  if (!isCommitSha(mergeBase)) {
    throw new Error(`cannot resolve the merge base of ${base} and ${head}`);
  }
  return mergeBase;
}

export function listChangedFiles({ base, head, cwd = process.cwd() }) {
  const mergeBase = resolveScopeMergeBase({ base, head, cwd });
  let output;
  try {
    output = execFileSync(
      "git",
      ["diff", "--name-only", "--diff-filter=d", "-z", mergeBase, head, "--"],
      {
        cwd,
        encoding: "utf8",
        maxBuffer: 64 * 1024 * 1024,
      },
    );
  } catch (error) {
    throw new Error(
      `cannot list files changed between ${mergeBase} and ${head}`,
    );
  }
  return [...new Set(output.split("\0").filter((entry) => entry.length > 0))];
}

export function listSelectedFiles({
  all = false,
  base = null,
  head = null,
  cwd = process.cwd(),
} = {}) {
  if (all && (base || head)) {
    throw new Error("--all cannot be combined with --base/--head");
  }
  if ((base && !head) || (!base && head)) {
    throw new Error("--base and --head must be given together");
  }
  if (base && head) return listChangedFiles({ base, head, cwd });
  const args = all
    ? ["ls-files", "-z"]
    : ["diff", "--cached", "--name-only", "--diff-filter=d", "-z"];
  const output = execFileSync("git", args, {
    cwd,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  return [...new Set(output.split("\0").filter((entry) => entry.length > 0))];
}

export function checkFiles({
  cwd = process.cwd(),
  all = false,
  base = null,
  head = null,
  excludes = [],
} = {}) {
  const violations = [];
  const errors = [];
  const files = listSelectedFiles({ all, base, head, cwd });
  for (const file of files) {
    if (isExcluded(file, excludes)) continue;
    const path = join(cwd, file);
    let size;
    let sample;
    try {
      const stats = statSync(path);
      if (stats.isDirectory()) continue;
      size = stats.size;
      sample = readSample(path, size);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      errors.push(`${file}: ${error.message}`);
      continue;
    }
    if (isBinarySample(sample)) continue;
    const tokens = estimateTokens(size);
    if (tokens > MAX_TOKENS) violations.push({ file, tokens });
  }
  return { violations, errors };
}

export function formatReport(violations) {
  if (violations.length === 0) return "";
  const lines = [
    "",
    `ERROR: The following files exceed the ${MAX_TOKENS}-token limit:`,
    "",
  ];
  for (const { file, tokens } of violations) {
    lines.push(`  ${file} (~${tokens} tokens)`);
  }
  lines.push(
    "",
    "Please split these files into smaller modules or test files.",
  );
  return `${lines.join("\n")}\n`;
}

function readPatternFile(file) {
  return readFileSync(file, "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#"));
}

function parseArgs(argv) {
  const options = {
    all: false,
    base: null,
    head: null,
    excludes: [],
    excludeFiles: [],
    cwd: process.cwd(),
    help: false,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      const value = argv[index + 1];
      if (value === undefined) throw new Error(`${arg} requires a value`);
      index += 1;
      return value;
    };
    if (arg === "--all") options.all = true;
    else if (arg === "--base") options.base = next();
    else if (arg === "--head") options.head = next();
    else if (arg === "--exclude") options.excludes.push(next());
    else if (arg === "--exclude-from") options.excludeFiles.push(next());
    else if (arg === "--cwd") options.cwd = resolve(next());
    else if (arg === "-h" || arg === "--help") options.help = true;
    else throw new Error(`unknown argument: ${arg}`);
  }
  // Relative pattern files resolve inside the selected checkout, not the
  // process directory, so `--cwd other-repo --exclude-from .excludes` works
  // regardless of argument order.
  for (const file of options.excludeFiles) {
    options.excludes.push(...readPatternFile(resolve(options.cwd, file)));
  }
  delete options.excludeFiles;
  return options;
}

if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`check-file-tokens: ${error.message}`);
    console.error(USAGE);
    process.exitCode = 1;
  }
  if (options?.help) {
    console.log(USAGE);
  } else if (options) {
    try {
      const { violations, errors } = checkFiles(options);
      if (errors.length) throw new Error(errors.join("\n"));
      process.stdout.write(formatReport(violations, options.maxTokens));
      process.exitCode = violations.length > 0 ? 1 : 0;
    } catch (error) {
      console.error(`check-file-tokens: ${error.message}`);
      process.exitCode = 1;
    }
  }
}
