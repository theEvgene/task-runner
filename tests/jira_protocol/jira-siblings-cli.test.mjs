import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import test from "node:test";

function runSiblings(origin, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      ["scripts/jira-protocol.mjs", "siblings", ...args],
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

function state() {
  const issues = {
    "LEL-42": {
      id: "42",
      parent: { id: "7", key: "LEL-7" },
      summary: "Source",
      status: { id: "1", name: "Open" },
      assignee: { name: "owner", key: "owner" },
    },
    "LEL-7": {
      id: "7",
      parent: null,
      summary: "Parent",
      subtasks: ["LEL-42", "LEL-43", "LEL-44"],
    },
    "LEL-43": {
      id: "43",
      summary: "Sibling work",
      status: { id: "3", name: "In Progress" },
      assignee: { name: "dev.user", key: "dev.user" },
    },
    "LEL-44": {
      id: "44",
      summary: "Unassigned sibling",
      status: { id: "1", name: "Open" },
      assignee: null,
    },
  };
  return { issues, parentStatus: 200 };
}

function handler(shared, origin) {
  return (request, response) => {
    const url = new URL(request.url, origin);
    response.setHeader("content-type", "application/json");
    const issueMatch = url.pathname.match(/^\/rest\/api\/2\/issue\/(LEL-\d+)$/);
    const commentMatch = url.pathname.match(
      /^\/rest\/api\/2\/issue\/(LEL-\d+)\/comment$/
    );
    if (commentMatch) {
      response.end(
        JSON.stringify({ startAt: 0, maxResults: 50, total: 0, comments: [] })
      );
      return;
    }
    if (issueMatch) {
      const key = issueMatch[1];
      if (key === "LEL-7" && shared.parentStatus !== 200) {
        response.statusCode = shared.parentStatus;
        response.end(JSON.stringify({ errorMessages: ["denied"] }));
        return;
      }
      const found = shared.issues[key];
      if (!found) {
        response.statusCode = 404;
        response.end(JSON.stringify({ errorMessages: ["missing"] }));
        return;
      }
      const fields = {
        summary: found.summary,
        updated: "2026-11-15T12:00:00.000+0300",
        attachment: [],
        issuelinks: [],
        parent: found.parent === undefined ? null : found.parent,
      };
      if (found.subtasks)
        fields.subtasks = found.subtasks.map((child) => ({ key: child }));
      if (found.status) fields.status = found.status;
      if (found.assignee !== undefined) fields.assignee = found.assignee;
      response.end(
        JSON.stringify({
          id: found.id,
          key,
          self: `${origin}/rest/api/2/issue/${key}`,
          fields,
        })
      );
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ errorMessages: ["unexpected"] }));
  };
}

async function withServer(t, shared, run) {
  let origin;
  const server = createServer((request, response) =>
    handler(shared, origin)(request, response)
  );
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  origin = `http://127.0.0.1:${server.address().port}`;
  return run(origin);
}

test("siblings CLI returns matches with links and preserves the exit-6 contract", async (t) => {
  await withServer(t, state(), async (origin) => {
    const complete = await runSiblings(origin, [
      "--issue",
      "LEL-42",
      "--assignee",
      "dev.user",
    ]);
    assert.equal(complete.status, 0, complete.stderr || complete.stdout);
    const receipt = JSON.parse(complete.stdout);
    assert.equal(receipt.operation, "siblings");
    assert.equal(receipt.completeness.comprehensive, true);
    assert.deepEqual(
      receipt.matches.map((entry) => entry.key),
      ["LEL-43"]
    );
    assert.equal(receipt.matches[0].summary, "Sibling work");
    assert.equal(receipt.matches[0].status, "In Progress");
    assert.equal(receipt.matches[0].link, `${origin}/browse/LEL-43`);
    assert.deepEqual(receipt.unassigned, ["LEL-44"]);
    assert.doesNotMatch(complete.stdout, /loopback-session/);
  });
});

test("siblings CLI reports parent denial as incomplete, not an empty result", async (t) => {
  const shared = state();
  shared.parentStatus = 403;
  await withServer(t, shared, async (origin) => {
    const result = await runSiblings(origin, [
      "--issue",
      "LEL-42",
      "--assignee",
      "dev.user",
    ]);
    assert.equal(result.status, 6, result.stderr || result.stdout);
    const receipt = JSON.parse(result.stdout);
    assert.equal(receipt.completeness.comprehensive, false);
    assert.equal(
      receipt.completeness.categories.parent.reason,
      "access_denied"
    );
    assert.deepEqual(receipt.matches, []);
  });
});

test("siblings CLI rejects a bad login without a session call", async (t) => {
  let requests = 0;
  const server = createServer((request, response) => {
    requests += 1;
    response.statusCode = 500;
    response.end("{}");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const result = await runSiblings(origin, [
    "--issue",
    "LEL-42",
    "--assignee",
    "not a login!",
  ]);
  assert.equal(result.status, 2);
  assert.equal(requests, 0);
  assert.match(result.stdout, /assignee must be a Jira login/);
});
