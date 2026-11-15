import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import test from "node:test";

function runExtract(origin, issueKey) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      ["scripts/jira-protocol.mjs", "extract", "--issue", issueKey],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          JIRA_BASE_URL: origin,
          JIRA_PROTOCOL_TEST_MODE: "1",
          JIRA_SESSION_COOKIE: "loopback-session",
        },
        stdio: ["ignore", "pipe", "pipe"],
      }
    );
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("error", reject);
    child.on("close", (status, signal) =>
      resolve({ status, signal, stdout, stderr })
    );
  });
}

test("extract CLI stops on access denial", async (t) => {
  let requests = 0;
  const server = createServer((request, response) => {
    requests += 1;
    response.writeHead(403, { "content-type": "application/json" });
    response.end(JSON.stringify({ errorMessages: ["forbidden"] }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const result = await runExtract(
    `http://127.0.0.1:${server.address().port}`,
    "LEL-42"
  );
  assert.notEqual(result.status, 0);
  assert.equal(requests, 1);
  assert.doesNotMatch(result.stdout + result.stderr, /loopback-session/);
});

test("extract CLI uses real loopback transport and preserves complete and exit-6 contracts", async (t) => {
  const requests = [];
  let origin;
  const server = createServer((request, response) => {
    requests.push({
      url: request.url,
      cookie: request.headers.cookie,
      remoteAddress: request.socket.remoteAddress,
    });
    const url = new URL(request.url, origin);
    const issueMatch = url.pathname.match(
      /^\/rest\/api\/2\/issue\/(LEL-(?:42|43))$/
    );
    const commentMatch = url.pathname.match(
      /^\/rest\/api\/2\/issue\/(LEL-(?:42|43))\/comment$/
    );
    response.setHeader("content-type", "application/json");
    if (issueMatch) {
      const key = issueMatch[1];
      const fields = {
        summary: `${key} summary`,
        updated: "2026-11-15T12:00:00.000+0300",
        attachment: [
          {
            id: "9",
            filename: "proof.txt",
            content: `${origin}/secure/attachment/9/proof.txt?token=secret-value`,
          },
        ],
        issuelinks: [],
      };
      if (key === "LEL-42") fields.parent = null;
      response.end(
        JSON.stringify({
          id: key.slice(4),
          key,
          self: `${origin}/rest/api/2/issue/${key}`,
          fields,
        })
      );
      return;
    }
    if (commentMatch) {
      response.end(
        JSON.stringify({
          startAt: Number(url.searchParams.get("startAt")),
          maxResults: Number(url.searchParams.get("maxResults")),
          total: 0,
          comments: [],
        })
      );
      return;
    }
    response.statusCode = 404;
    response.end(
      JSON.stringify({ errorMessages: ["unexpected fixture path"] })
    );
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();
  origin = `http://127.0.0.1:${address.port}`;

  const complete = await runExtract(origin, "LEL-42");
  assert.equal(complete.status, 0, complete.stderr || complete.stdout);
  const completeReceipt = JSON.parse(complete.stdout);
  assert.equal(completeReceipt.completeness.comprehensive, true);
  assert.match(
    completeReceipt.issue.attachments[0].content.url,
    /\[REDACTED\]/
  );
  assert.doesNotMatch(complete.stdout, /secret-value/);

  const incomplete = await runExtract(origin, "LEL-43");
  assert.equal(incomplete.status, 6, incomplete.stderr || incomplete.stdout);
  const incompleteReceipt = JSON.parse(incomplete.stdout);
  assert.equal(incompleteReceipt.parent.presence, "unobserved");
  assert.equal(incompleteReceipt.completeness.comprehensive, false);

  assert.equal(requests.length, 6);
  assert.equal(
    requests.every((request) => request.cookie === "loopback-session"),
    true
  );
  assert.equal(
    requests.every((request) =>
      ["127.0.0.1", "::ffff:127.0.0.1"].includes(request.remoteAddress)
    ),
    true
  );
  for (const key of ["LEL-42", "LEL-43"]) {
    const issueRequests = requests.filter((request) =>
      request.url.startsWith(`/rest/api/2/issue/${key}?`)
    );
    assert.equal(issueRequests.length, 2);
    for (const request of issueRequests) {
      const url = new URL(request.url, origin);
      assert.equal(url.searchParams.get("fields"), "*all");
      assert.equal(url.searchParams.get("expand"), "names,schema");
    }
    const commentRequest = requests.find((request) =>
      request.url.startsWith(`/rest/api/2/issue/${key}/comment?`)
    );
    const commentUrl = new URL(commentRequest.url, origin);
    assert.equal(commentUrl.searchParams.get("startAt"), "0");
    assert.equal(commentUrl.searchParams.get("maxResults"), "50");
  }
});
