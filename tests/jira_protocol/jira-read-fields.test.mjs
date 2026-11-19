import assert from "node:assert/strict";
import test from "node:test";
import {
  formatForQueue,
  parseFieldList,
  readCompactFields,
} from "../../scripts/jira_protocol/read-fields.mjs";
import { JiraProtocolError } from "../../scripts/jira_protocol/protocol.mjs";

function transportFor(payload, { onRequest } = {}) {
  return {
    origin: "https://jira3.tcsbank.ru",
    calls: [],
    async request(request) {
      this.calls.push(request);
      if (onRequest) onRequest(request);
      return { payload: structuredClone(payload) };
    },
  };
}

function basePayload(fields) {
  return {
    id: "42",
    key: "LEL-42",
    self: "https://jira3.tcsbank.ru/rest/api/2/issue/LEL-42",
    fields,
  };
}

test("returns selected values without comments or parent extraction", async () => {
  const transport = transportFor(
    basePayload({
      summary: "Fix edge",
      status: { name: "In Progress", id: "3" },
      assignee: { name: "dev.user", displayName: "Dev" },
    })
  );
  const result = await readCompactFields(transport, {
    issueKey: "lel-42",
    fields: "summary,status,assignee",
  });
  assert.equal(result.schema, "jira-protocol-result/2");
  assert.equal(result.operation, "read-fields");
  assert.deepEqual(result.requested, ["summary", "status", "assignee"]);
  assert.deepEqual(result.fields.summary, {
    state: "value",
    value: "Fix edge",
  });
  assert.deepEqual(result.fields.status, {
    state: "value",
    value: { name: "In Progress", id: "3" },
  });
  assert.deepEqual(result.fields.assignee, {
    state: "value",
    value: { login: "dev.user", displayName: "Dev" },
  });
  assert.equal(result.commentsFetched, false);
  assert.equal(result.parentFetched, false);
  assert.equal(result.completeness.comprehensive, true);
  assert.equal(transport.calls.length, 1);
  assert.match(transport.calls[0].path, /LEL-42$/);
  assert.equal(transport.calls[0].query.fields, "summary,status,assignee");
  assert.ok(!transport.calls[0].path.endsWith("/comment"));
});

test("requests parent only when selected and keeps short entries", async () => {
  const transport = transportFor(
    basePayload({
      summary: "Child",
      parent: { key: "LEL-7", fields: { summary: "Epic title" } },
      subtasks: [
        {
          key: "LEL-43",
          fields: { summary: "Sub one", status: { name: "Open" } },
        },
      ],
    })
  );
  const result = await readCompactFields(transport, {
    issueKey: "LEL-42",
    fields: "summary,parent,subtasks",
  });
  assert.equal(result.parentFetched, true);
  assert.deepEqual(result.fields.parent, {
    state: "value",
    value: { key: "LEL-7", summary: "Epic title" },
  });
  assert.deepEqual(result.fields.subtasks, {
    state: "value",
    value: [{ key: "LEL-43", summary: "Sub one", status: "Open" }],
  });
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0].query.fields, "summary,parent,subtasks");
});

test("rejects unknown and empty field lists", () => {
  assert.throws(() => parseFieldList("summary,bogus"), /unknown field: bogus/);
  assert.throws(() => parseFieldList("bogus"), /supported: /);
  assert.throws(() => parseFieldList(""), /no fields requested/);
  assert.deepEqual(parseFieldList("summary,summary "), ["summary"]);
});

test("distinguishes empty, unobserved, and terminal denial", async () => {
  const transport = transportFor(basePayload({ summary: "T", assignee: null }));
  const result = await readCompactFields(transport, {
    issueKey: "LEL-42",
    fields: "summary,assignee,status",
  });
  assert.deepEqual(result.fields.assignee, { state: "empty", value: null });
  assert.deepEqual(result.fields.status, {
    state: "unobserved",
    value: null,
  });
  assert.equal(result.completeness.comprehensive, false);
  assert.ok(
    result.completeness.visibilityLimits.some((entry) =>
      entry.startsWith("status:")
    )
  );
  assert.ok(
    result.completeness.gaps.some((entry) => entry.category === "status")
  );

  const denied = {
    origin: "https://jira3.tcsbank.ru",
    async request() {
      throw new JiraProtocolError("denied", { status: 403 });
    },
  };
  await assert.rejects(
    () =>
      readCompactFields(denied, {
        issueKey: "LEL-42",
        fields: "summary",
      }),
    (error) => error instanceof JiraProtocolError && error.status === 403
  );
});

test("flagged maps custom field null to false and array to true", async () => {
  const off = transportFor(basePayload({ customfield_10641: null }));
  const offResult = await readCompactFields(off, {
    issueKey: "LEL-42",
    fields: "flagged",
  });
  assert.deepEqual(offResult.fields.flagged, {
    state: "value",
    value: false,
  });
  assert.equal(off.calls[0].query.fields, "customfield_10641");

  const on = transportFor(
    basePayload({ customfield_10641: [{ value: "Impediment" }] })
  );
  const onResult = await readCompactFields(on, {
    issueKey: "LEL-42",
    fields: "flagged",
  });
  assert.deepEqual(onResult.fields.flagged, {
    state: "value",
    value: true,
  });
});

test("queue truncation never claims completeness and keeps no secrets", () => {
  const result = {
    schema: "jira-protocol-result/2",
    operation: "read-fields",
    dataContract: { kind: "compact-selected-fields", comprehensive: true },
    key: "LEL-42",
    requested: ["summary"],
    fields: { summary: { state: "value", value: "x".repeat(500) } },
    completeness: {
      comprehensive: true,
      categories: { summary: { status: "complete" } },
      gaps: [],
      visibilityLimits: [],
      truncated: false,
      omitted: [],
    },
  };
  const kept = formatForQueue(result, { maxBytes: 100000 });
  assert.equal(kept.truncated, false);
  const cut = formatForQueue(result, { maxBytes: 10 });
  assert.equal(cut.truncated, true);
  const skeleton = JSON.parse(cut.json);
  assert.equal(skeleton.completeness.comprehensive, false);
  assert.equal(skeleton.completeness.truncated, true);
  assert.deepEqual(skeleton.fields.summary, {
    state: "truncated",
    value: null,
  });
  assert.ok(
    skeleton.completeness.omitted.some((entry) =>
      entry.startsWith("output-truncated-at-")
    )
  );
});
