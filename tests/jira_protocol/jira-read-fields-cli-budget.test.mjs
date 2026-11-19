import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import {
  assertBudgetedCli,
  assertRawStdoutMeasured,
  assertStdoutWithinBudget,
  runNodeCli,
  utf8ByteLength,
} from "./cli-byte-budget.mjs";

const CYRILLIC_SUMMARY = "а".repeat(100);
const COOKIE = "loopback-session-secret";

function readFieldsCli(origin, extraArgs) {
  return runNodeCli(
    ["scripts/jira-protocol.mjs", "read-fields", ...extraArgs],
    {
      env: {
        JIRA_BASE_URL: origin,
        JIRA_PROTOCOL_TEST_MODE: "1",
        JIRA_SESSION_COOKIE: COOKIE,
      },
    }
  );
}

async function withReadFieldsServer(t, summary, run) {
  let origin;
  const server = createServer((request, response) => {
    const url = new URL(request.url, origin);
    response.setHeader("content-type", "application/json");
    const match = url.pathname.match(/^\/rest\/api\/2\/issue\/(LEL-\d+)$/);
    if (!match) {
      response.statusCode = 404;
      response.end(JSON.stringify({ errorMessages: ["unexpected"] }));
      return;
    }
    const key = match[1];
    response.end(
      JSON.stringify({
        id: "42",
        key,
        self: `${origin}/rest/api/2/issue/${key}`,
        fields: { summary },
      })
    );
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  origin = `http://127.0.0.1:${server.address().port}`;
  return run(origin);
}

test("byte helper measures emitted stdout bytes, not string length", async () => {
  const result = await runNodeCli(["--eval", 'process.stdout.write("а\\n")']);
  assert.equal(result.status, 0);
  // "а" is 1 UTF-16 unit but 2 UTF-8 bytes; plus "\n" => 2 chars, 3 bytes.
  assert.equal(result.stdout, "а\n");
  assert.equal(result.stdout.length, 2);
  assert.equal(result.stdoutBytes, 3);
  assert.equal(utf8ByteLength(result.stdout), 3);
  assertRawStdoutMeasured(result);
});

test("read-fields CLI fit stays complete within budget", async (t) => {
  await withReadFieldsServer(t, CYRILLIC_SUMMARY, async (origin) => {
    const fit = await assertBudgetedCli({
      run: () =>
        readFieldsCli(origin, ["--issue", "LEL-42", "--fields", "summary"]),
      maxBytes: Number.MAX_SAFE_INTEGER,
      expect: "complete",
      isComplete: (receipt) =>
        receipt.completeness.comprehensive === true &&
        receipt.fields.summary.value === CYRILLIC_SUMMARY,
    });
    assert.equal(fit.status, 0, fit.stderr || fit.stdout);
    assert.ok(fit.stdout.endsWith("\n"), "stdout must include final newline");
    assert.doesNotMatch(fit.stdout + fit.stderr, /loopback-session-secret/);
  });
});

test("read-fields CLI truncates multibyte output that fits chars but not bytes", async (t) => {
  await withReadFieldsServer(t, CYRILLIC_SUMMARY, async (origin) => {
    const fit = await readFieldsCli(origin, [
      "--issue",
      "LEL-42",
      "--fields",
      "summary",
    ]);
    assert.equal(fit.status, 0, fit.stderr || fit.stdout);
    const fitBytes = assertRawStdoutMeasured(fit);
    const compact = `${JSON.stringify(JSON.parse(fit.stdout))}\n`;
    const compactChars = compact.length;
    const compactBytes = utf8ByteLength(compact);
    assert.ok(
      compactBytes > compactChars,
      "fixture must carry multibyte expansion"
    );
    // Budget fits the character count but not the byte count of the same
    // serialization: proves the boundary is bytes, including the newline.
    // One byte below the full compact size still leaves room for the
    // truncated skeleton (which drops the field values), so the CLI must
    // emit an explicitly truncated receipt within budget (exit 6).
    const maxBytes = compactBytes - 1;
    assert.ok(
      compactChars <= maxBytes,
      `need chars (${compactChars}) to fit budget (${maxBytes})`
    );
    assert.ok(
      compactBytes > maxBytes,
      `need bytes (${compactBytes}) to exceed budget (${maxBytes})`
    );
    assert.ok(fitBytes > 0, `sanity: fit run emitted ${fitBytes} stdout bytes`);

    const cut = await assertBudgetedCli({
      run: () =>
        readFieldsCli(origin, [
          "--issue",
          "LEL-42",
          "--fields",
          "summary",
          "--max-bytes",
          String(maxBytes),
        ]),
      maxBytes,
      expect: "truncated",
      isTruncated: (receipt) =>
        receipt.completeness.truncated === true &&
        receipt.completeness.comprehensive === false &&
        receipt.fields.summary.state === "truncated",
    });
    assert.equal(cut.status, 6, cut.stderr || cut.stdout);
    assert.ok(cut.stdout.endsWith("\n"));
    assert.doesNotMatch(cut.stdout + cut.stderr, /loopback-session-secret/);
  });
});

test("read-fields CLI refuses a budget too small for the fallback", async (t) => {
  await withReadFieldsServer(t, CYRILLIC_SUMMARY, async (origin) => {
    const refused = await assertBudgetedCli({
      run: () =>
        readFieldsCli(origin, [
          "--issue",
          "LEL-42",
          "--fields",
          "summary",
          "--max-bytes",
          "10",
        ]),
      maxBytes: 10,
      expect: "refused",
      forbiddenPayload: CYRILLIC_SUMMARY,
    });
    assert.equal(refused.status, 2, refused.stderr || refused.stdout);
    assert.match(refused.stdout + refused.stderr, /max-bytes/);
    // Refusal is an explicit error receipt, never the oversized payload.
    const receipt = JSON.parse(refused.stdout);
    assert.equal(receipt.state, "Failed");
    assert.doesNotMatch(
      refused.stdout + refused.stderr,
      /loopback-session-secret/
    );
  });
});

test("read-fields CLI keeps exit codes and redaction with generous budget", async (t) => {
  await withReadFieldsServer(t, "plain summary", async (origin) => {
    const generous = await assertBudgetedCli({
      run: () =>
        readFieldsCli(origin, [
          "--issue",
          "LEL-42",
          "--fields",
          "summary",
          "--max-bytes",
          "100000",
        ]),
      maxBytes: 100000,
      expect: "complete",
      isComplete: (receipt) =>
        receipt.completeness.comprehensive === true &&
        receipt.fields.summary.value === "plain summary",
    });
    assert.equal(generous.status, 0, generous.stderr || generous.stdout);
    assertStdoutWithinBudget(generous.stdout, 100000);
  });
});
