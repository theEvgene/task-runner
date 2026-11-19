import assert from "node:assert/strict";
import { spawn } from "node:child_process";

/** UTF-8 byte length of a decoded string. */
export function utf8ByteLength(text) {
  return Buffer.byteLength(text, "utf8");
}

/**
 * Run a Node CLI and capture the actual emitted stdout bytes.
 *
 * Chunks are collected as Buffers so `stdoutBytes` is the true wire size,
 * not `stdout.length` (UTF-16 code units). `stdout` is the UTF-8 decode of
 * those exact bytes, so `stdoutBytes === utf8ByteLength(stdout)` for
 * well-formed output.
 */
export function runNodeCli(nodeArgs, { env = {}, cwd = process.cwd() } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, nodeArgs, {
      cwd,
      env: { ...process.env, ...env },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const outChunks = [];
    const errChunks = [];
    child.stdout.on("data", (chunk) => outChunks.push(chunk));
    child.stderr.on("data", (chunk) => errChunks.push(chunk));
    child.on("error", reject);
    child.on("close", (status, signal) => {
      const stdoutRaw = Buffer.concat(outChunks);
      const stdout = stdoutRaw.toString("utf8");
      resolve({
        status,
        signal,
        stdout,
        stdoutRaw,
        stdoutBytes: stdoutRaw.length,
        stderr: Buffer.concat(errChunks).toString("utf8"),
      });
    });
  });
}

/**
 * Prove the captured stdout was measured as bytes, not string length.
 * Returns the byte count.
 */
export function assertRawStdoutMeasured({ stdout, stdoutBytes }) {
  assert.equal(
    stdoutBytes,
    utf8ByteLength(stdout),
    `raw stdout bytes (${stdoutBytes}) must equal UTF-8 bytes of decoded stdout (${utf8ByteLength(stdout)})`
  );
  return stdoutBytes;
}

/** Assert decoded stdout fits the byte budget (newline included). */
export function assertStdoutWithinBudget(stdout, maxBytes, label = "stdout") {
  const bytes = utf8ByteLength(stdout);
  assert.ok(
    bytes <= maxBytes,
    `${label}: ${bytes} UTF-8 bytes exceed budget of ${maxBytes} bytes`
  );
  return bytes;
}

/**
 * Generic budgeted-CLI contract check, independent of any command.
 *
 * `run` must return `{ status, stdout, stdoutBytes, stderr }` as produced
 * by `runNodeCli`. `expect` selects the outcome:
 * - "complete": stdout parses, `isComplete(receipt)` is true, bytes fit.
 * - "truncated": stdout parses, `isTruncated(receipt)` is true, bytes fit.
 * - "refused": nonzero exit, stdout must not contain `forbiddenPayload`
 *   (the oversized original value); the refusal itself is explicit.
 */
export async function assertBudgetedCli({
  run,
  maxBytes,
  expect,
  isComplete,
  isTruncated,
  forbiddenPayload,
}) {
  const result = await run();
  assertRawStdoutMeasured(result);
  if (expect === "complete" || expect === "truncated") {
    const bytes = assertStdoutWithinBudget(result.stdout, maxBytes);
    const receipt = JSON.parse(result.stdout);
    if (expect === "complete") {
      assert.ok(
        isComplete(receipt),
        "expected a complete receipt within budget"
      );
    } else {
      assert.ok(
        isTruncated(receipt),
        "expected an explicitly truncated/incomplete receipt within budget"
      );
    }
    return { ...result, bytes, receipt };
  }
  if (expect === "refused") {
    assert.notEqual(result.status, 0, "refusal must exit nonzero");
    if (forbiddenPayload !== undefined) {
      assert.ok(
        !result.stdout.includes(forbiddenPayload),
        "refusal must not emit the oversized payload"
      );
    }
    return result;
  }
  throw new Error(`unknown expect: ${expect}`);
}
