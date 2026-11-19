import assert from "node:assert/strict";
import test from "node:test";
import {
  assertAssigneeLogin,
  findSiblingIssues,
} from "../../scripts/jira_protocol/siblings.mjs";
import { JiraProtocolError } from "../../scripts/jira_protocol/protocol.mjs";

const SECRET_COOKIE = "secret-cookie-value";
const SECRET_TOKEN = "secret-atl-token";

function issue(key, id, fields = {}) {
  return {
    id: String(id),
    key,
    self: `https://jira3.tcsbank.ru/rest/api/2/issue/${id}`,
    fields: {
      summary: `${key} summary`,
      updated: "2026-11-15T12:00:00.000+0300",
      attachment: [],
      issuelinks: [],
      ...fields,
    },
  };
}

function child(key, id, { assignee = null, summary, status } = {}) {
  return issue(key, id, {
    parent: { id: "7", key: "LEL-7" },
    ...(summary === undefined ? {} : { summary }),
    ...(status === undefined
      ? { status: { id: "3", name: "In Progress" } }
      : { status }),
    assignee,
  });
}

class SiblingTransport {
  constructor({ issues, commentTotal = 0 }) {
    this.origin = "https://jira3.tcsbank.ru";
    this.issues = issues;
    this.commentTotal = commentTotal;
    this.calls = [];
  }

  async request(request) {
    this.calls.push(structuredClone(request));
    const commentMatch = request.path.match(
      /^\/rest\/api\/2\/issue\/(LEL-\d+)\/comment$/
    );
    if (commentMatch)
      return {
        payload: {
          startAt: 0,
          maxResults: 50,
          total: this.commentTotal,
          comments: [],
        },
      };
    const issueMatch = request.path.match(/^\/rest\/api\/2\/issue\/(LEL-\d+)$/);
    if (issueMatch) {
      const value = this.issues[issueMatch[1]];
      if (value instanceof Error) throw value;
      if (!value) throw new Error(`missing issue fixture ${issueMatch[1]}`);
      return { payload: structuredClone(value) };
    }
    throw new Error(`unexpected request ${request.path}`);
  }
}

function parentWith(...keys) {
  return issue("LEL-7", 7, {
    parent: null,
    subtasks: keys.map((key) => ({ id: key.slice(4), key })),
  });
}

test("assignee login validation mirrors the assign command", () => {
  assert.equal(assertAssigneeLogin("  dev.user "), "dev.user");
  for (const bad of [undefined, "", "  ", "a b", "a/b"]) {
    assert.throws(() => assertAssigneeLogin(bad), JiraProtocolError);
  }
  assert.throws(() => assertAssigneeLogin("x".repeat(300) + "!"), /login/);
});

test("matches exact login on either identity, excludes the source, lists unassigned", async () => {
  const transport = new SiblingTransport({
    issues: {
      "LEL-42": issue("LEL-42", 42, {
        parent: { id: "7", key: "LEL-7" },
      }),
      "LEL-7": parentWith("LEL-42", "LEL-43", "LEL-44", "LEL-45"),
      "LEL-43": child("LEL-43", 43, {
        assignee: { name: "dev.user", key: "other", displayName: "Dev" },
        summary: "By name",
      }),
      "LEL-44": child("LEL-44", 44, {
        assignee: { name: "other", key: "dev.user", displayName: "Dev" },
        summary: "By key",
      }),
      "LEL-45": child("LEL-45", 45, { assignee: null }),
    },
  });
  const result = await findSiblingIssues(transport, {
    issueKey: "lel-42",
    assignee: "dev.user",
  });
  assert.equal(result.schema, "jira-protocol-result/2");
  assert.equal(result.operation, "siblings");
  assert.equal(result.dataContract.comprehensive, true);
  assert.equal(result.completeness.comprehensive, true);
  assert.deepEqual(
    result.matches.map((entry) => entry.key),
    ["LEL-43", "LEL-44"]
  );
  for (const entry of result.matches) {
    assert.deepEqual(Object.keys(entry).sort(), [
      "key",
      "link",
      "status",
      "statusId",
      "summary",
    ]);
    assert.equal(entry.link, `https://jira3.tcsbank.ru/browse/${entry.key}`);
  }
  assert.deepEqual(result.unassigned, ["LEL-45"]);
  assert.deepEqual(result.unreadable, []);
  assert.equal(result.examined, 3);
  assert.equal(result.parent.key, "LEL-7");
  const leaked = JSON.stringify(result);
  assert.doesNotMatch(leaked, /secret-cookie-value|secret-atl-token/);
});

test("no matches and an absent parent are complete empty results", async () => {
  const noMatch = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": parentWith("LEL-42"),
        "LEL-43": child("LEL-43", 43, {
          assignee: { name: "someone.else", key: "someone.else" },
        }),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.deepEqual(noMatch.matches, []);
  assert.equal(noMatch.completeness.comprehensive, true);

  const noParent = await findSiblingIssues(
    new SiblingTransport({
      issues: { "LEL-42": issue("LEL-42", 42, { parent: null }) },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.deepEqual(noParent.matches, []);
  assert.equal(noParent.parent.presence, "absent");
  assert.equal(noParent.completeness.comprehensive, true);
});

test("unobserved or malformed parent fields fail closed without children", async () => {
  const withoutParent = issue("LEL-42", 42);
  delete withoutParent.fields.parent;
  const unobserved = await findSiblingIssues(
    new SiblingTransport({ issues: { "LEL-42": withoutParent } }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.equal(unobserved.parent.presence, "unobserved");
  assert.equal(unobserved.completeness.comprehensive, false);
  assert.deepEqual(unobserved.matches, []);

  const cyclic = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, { parent: { key: "LEL-42" } }),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.equal(cyclic.completeness.categories.parent.reason, "parent_cycle");
  assert.equal(cyclic.completeness.comprehensive, false);
});

test("unavailable parent and child lists report incompleteness, not results", async () => {
  const denied = (status) =>
    new JiraProtocolError(`Jira returned HTTP ${status}`, { status });

  const missing = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": denied(404),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.equal(
    missing.completeness.categories.parent.reason,
    "not_found_or_not_visible"
  );
  assert.deepEqual(missing.matches, []);
  assert.equal(missing.completeness.comprehensive, false);

  const forbidden = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": denied(403),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.equal(
    forbidden.completeness.categories.parent.reason,
    "access_denied"
  );
  assert.equal(forbidden.completeness.categories.parent.httpStatus, 403);
  assert.equal(forbidden.completeness.comprehensive, false);

  const noList = issue("LEL-7", 7, { parent: null });
  delete noList.fields.subtasks;
  const unlisted = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": noList,
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.equal(
    unlisted.completeness.categories.childList.reason,
    "field_unobserved_permission_or_configuration"
  );
  assert.equal(unlisted.completeness.comprehensive, false);

  const malformed = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": issue("LEL-7", 7, {
          parent: null,
          subtasks: [{ id: "43" }, { id: "44", key: "LEL-44" }],
        }),
        "LEL-44": child("LEL-44", 44, {
          assignee: { name: "dev.user", key: "dev.user" },
        }),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.equal(
    malformed.completeness.categories.childList.reason,
    "malformed_child_entries"
  );
  assert.deepEqual(
    malformed.matches.map((entry) => entry.key),
    ["LEL-44"]
  );
  assert.equal(malformed.completeness.comprehensive, false);
});

test("child read errors are reported per child without hiding the rest", async () => {
  const denied = (status) =>
    new JiraProtocolError(`Jira returned HTTP ${status}`, { status });
  const result = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": parentWith("LEL-42", "LEL-43", "LEL-44", "LEL-45"),
        "LEL-43": denied(404),
        "LEL-44": denied(403),
        "LEL-45": child("LEL-45", 45, {
          assignee: { name: "dev.user", key: "dev.user" },
        }),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.deepEqual(
    result.unreadable.map((entry) => [entry.key, entry.reason]),
    [
      ["LEL-43", "not_found_or_not_visible"],
      ["LEL-44", "access_denied"],
    ]
  );
  assert.deepEqual(
    result.matches.map((entry) => entry.key),
    ["LEL-45"]
  );
  assert.equal(
    result.completeness.categories.childReads.reason,
    "child_reads_incomplete"
  );
  assert.equal(result.completeness.comprehensive, false);
});

test("source denial stays terminal and bad keys are rejected", async () => {
  const denied = new JiraProtocolError("Jira returned HTTP 403", {
    status: 403,
  });
  await assert.rejects(
    findSiblingIssues(new SiblingTransport({ issues: { "LEL-42": denied } }), {
      issueKey: "LEL-42",
      assignee: "dev.user",
    }),
    (error) => error.status === 403
  );
  await assert.rejects(
    findSiblingIssues(new SiblingTransport({ issues: {} }), {
      issueKey: "ABC-1",
      assignee: "dev.user",
    }),
    /LEL issue key/
  );
  await assert.rejects(
    findSiblingIssues(new SiblingTransport({ issues: {} }), {
      issueKey: "LEL-42",
      assignee: "not a login!",
    }),
    /assignee must be a Jira login/
  );
});

test("explicit null stays unassigned while missing and malformed identities are unreadable", async () => {
  const missing = child("LEL-44", 44, {
    assignee: { name: "dev.user", key: "dev.user" },
  });
  delete missing.fields.assignee;
  const malformed = child("LEL-45", 45, {
    assignee: { name: "dev.user", key: "dev.user" },
  });
  malformed.fields.assignee = "dev.user";
  const emptyIdentity = child("LEL-46", 46, {
    assignee: { name: "dev.user", key: "dev.user" },
  });
  emptyIdentity.fields.assignee = { name: "", key: "" };
  const result = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": parentWith("LEL-42", "LEL-43", "LEL-44", "LEL-45", "LEL-46"),
        "LEL-43": child("LEL-43", 43, { assignee: null }),
        "LEL-44": missing,
        "LEL-45": malformed,
        "LEL-46": emptyIdentity,
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.deepEqual(result.unassigned, ["LEL-43"]);
  assert.deepEqual(
    result.unreadable.map((entry) => [entry.key, entry.reason]),
    [
      ["LEL-44", "assignee_unobserved"],
      ["LEL-45", "malformed_assignee"],
      ["LEL-46", "assignee_identity_unobserved"],
    ]
  );
  assert.deepEqual(result.matches, []);
  assert.equal(result.completeness.comprehensive, false);
  assert.equal(
    result.completeness.categories.childReads.reason,
    "child_reads_incomplete"
  );
});

test("matching child without summary or status is unreadable, not a match", async () => {
  const noSummary = child("LEL-43", 43, {
    assignee: { name: "dev.user", key: "dev.user" },
  });
  delete noSummary.fields.summary;
  const noStatus = child("LEL-44", 44, {
    assignee: { name: "dev.user", key: "dev.user" },
  });
  delete noStatus.fields.status;
  const result = await findSiblingIssues(
    new SiblingTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { id: "7", key: "LEL-7" },
        }),
        "LEL-7": parentWith("LEL-42", "LEL-43", "LEL-44", "LEL-45"),
        "LEL-43": noSummary,
        "LEL-44": noStatus,
        "LEL-45": child("LEL-45", 45, {
          assignee: { name: "dev.user", key: "dev.user" },
          summary: "Good sibling",
        }),
      },
    }),
    { issueKey: "LEL-42", assignee: "dev.user" }
  );
  assert.deepEqual(
    result.matches.map((entry) => entry.key),
    ["LEL-45"]
  );
  assert.deepEqual(
    result.unreadable.map((entry) => [entry.key, entry.reason]),
    [
      ["LEL-43", "incomplete_child_content"],
      ["LEL-44", "incomplete_child_content"],
    ]
  );
  assert.deepEqual(result.unassigned, []);
  assert.equal(result.completeness.comprehensive, false);
});
