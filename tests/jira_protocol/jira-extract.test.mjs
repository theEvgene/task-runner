import assert from "node:assert/strict";
import test from "node:test";
import {
  collectComments,
  extractIssue,
  getIssueResource,
} from "../../scripts/jira_protocol/extract.mjs";
import { JiraProtocolError } from "../../scripts/jira_protocol/protocol.mjs";

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
      parent: null,
      ...fields,
    },
  };
}

class ExtractTransport {
  constructor({ issues, commentPages = {} }) {
    this.origin = "https://jira3.tcsbank.ru";
    this.issues = issues;
    this.commentPages = commentPages;
    this.calls = [];
  }

  async request(request) {
    this.calls.push(structuredClone(request));
    const commentMatch = request.path.match(
      /^\/rest\/api\/2\/issue\/(LEL-\d+)\/comment$/
    );
    if (commentMatch) {
      const key = commentMatch[1];
      const page = this.commentPages[key]?.[request.query.startAt];
      if (page instanceof Error) throw page;
      if (!page) throw new Error(`missing comment fixture ${key}`);
      return { payload: structuredClone(page) };
    }
    const issueMatch = request.path.match(/^\/rest\/api\/2\/issue\/(LEL-\d+)$/);
    if (issueMatch) {
      const value = this.issues[issueMatch[1]];
      if (value instanceof Error) throw value;
      if (!value) throw new Error(`missing issue fixture ${issueMatch[1]}`);
      const payload =
        typeof value === "function"
          ? value(this.calls)
          : structuredClone(value);
      return { payload };
    }
    throw new Error(`unexpected request ${request.path}`);
  }
}

const emptyComments = {
  startAt: 0,
  maxResults: 50,
  total: 0,
  comments: [],
};

test("extract reads every server-capped comment page and expands one parent", async () => {
  const transport = new ExtractTransport({
    issues: {
      "LEL-42": issue("LEL-42", 42, {
        parent: {
          id: "7",
          key: "LEL-7",
          fields: { summary: "Parent" },
        },
        attachment: [
          {
            id: "9",
            filename: "proof.txt",
            size: 12,
            mimeType: "text/plain",
            content:
              "https://jira3.tcsbank.ru/secure/attachment/9/proof.txt?token=secret",
            thumbnail: "https://cdn.invalid/proof.png",
          },
        ],
        issuelinks: [
          {
            id: "3",
            type: {
              id: "10000",
              name: "Blocks",
              inward: "is blocked by",
              outward: "blocks",
            },
            outwardIssue: {
              id: "99",
              key: "ABC-99",
              fields: { summary: "Linked" },
            },
          },
        ],
      }),
      "LEL-7": issue("LEL-7", 7),
    },
    commentPages: {
      "LEL-42": {
        0: {
          startAt: 0,
          maxResults: 1,
          total: 2,
          comments: [{ id: "1", body: "first" }],
        },
        1: {
          startAt: 1,
          maxResults: 1,
          total: 2,
          comments: [{ id: "2", body: "second" }],
        },
      },
      "LEL-7": { 0: emptyComments },
    },
  });

  const result = await extractIssue(transport, "lel-42", { pageSize: 50 });

  assert.equal(result.schema, "jira-protocol-result/2");
  assert.equal(result.dataContract.comprehensive, true);
  assert.equal(result.dataContract.fullContentProven, false);
  assert.deepEqual(
    result.issue.comments.map((comment) => comment.id),
    ["1", "2"]
  );
  assert.deepEqual(
    result.issue.pagination.comments.pages.map((page) => page.startAt),
    [0, 1]
  );
  assert.equal(result.parent.presence, "observed");
  assert.equal(result.parent.issue.key, "LEL-7");
  assert.equal(result.parent.furtherParent.presence, "absent");
  assert.equal(result.issue.issueLinks[0].direction, "outward");
  assert.equal(result.issue.issueLinks[0].issue.key, "ABC-99");
  assert.doesNotMatch(result.issue.attachments[0].content.url, /secret/);
  assert.equal(result.issue.attachments[0].content.status, "same_origin");
  assert.equal(
    result.issue.attachments[0].thumbnail.status,
    "cross_origin_unverified"
  );
  assert.equal(
    result.completeness.extendedContent.attachmentContent.status,
    "metadata_only_not_retrieved"
  );
  assert.equal(
    result.completeness.extendedContent.wiki.status,
    "unproven_not_retrieved"
  );
  assert.equal(
    transport.calls.some((call) => call.path.includes("attachment")),
    false
  );
  assert.equal(
    transport.calls.filter((call) => call.path === "/rest/api/2/issue/LEL-7")
      .length,
    2
  );
});

test("missing parent is unobserved while explicit null proves absence", async () => {
  const withoutParent = issue("LEL-42", 42);
  delete withoutParent.fields.parent;
  const missing = await extractIssue(
    new ExtractTransport({
      issues: { "LEL-42": withoutParent },
      commentPages: { "LEL-42": { 0: emptyComments } },
    }),
    "LEL-42"
  );
  assert.equal(missing.parent.presence, "unobserved");
  assert.equal(missing.completeness.categories.parent.status, "incomplete");
  assert.equal(missing.completeness.comprehensive, false);

  const absent = await extractIssue(
    new ExtractTransport({
      issues: { "LEL-42": issue("LEL-42", 42) },
      commentPages: { "LEL-42": { 0: emptyComments } },
    }),
    "LEL-42"
  );
  assert.equal(absent.parent.presence, "absent");
  assert.equal(absent.completeness.categories.parent.status, "complete");
});

test("parent 404 is not treated as no parent", async () => {
  const unavailable = new JiraProtocolError("Jira returned HTTP 404", {
    status: 404,
  });
  const result = await extractIssue(
    new ExtractTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          parent: { key: "LEL-7", fields: { summary: "Parent" } },
        }),
        "LEL-7": unavailable,
      },
      commentPages: { "LEL-42": { 0: emptyComments } },
    }),
    "LEL-42"
  );
  assert.equal(result.parent.presence, "observed");
  assert.equal(result.parent.retrieval, "failed");
  assert.equal(
    result.completeness.categories.parent.reason,
    "not_found_or_not_visible"
  );
  assert.equal(result.completeness.comprehensive, false);
});

test("permission denial remains terminal instead of becoming an empty collection", async () => {
  const denied = new JiraProtocolError("Jira returned HTTP 403", {
    status: 403,
  });
  const transport = new ExtractTransport({
    issues: { "LEL-42": issue("LEL-42", 42) },
    commentPages: { "LEL-42": { 0: denied } },
  });
  await assert.rejects(extractIssue(transport, "LEL-42"), (error) => {
    assert.equal(error.status, 403);
    return true;
  });
});

test("comment pagination fails closed on duplicates, changing totals, and limits", async () => {
  const fixtures = [
    {
      name: "duplicate_comment_id",
      pages: {
        0: { startAt: 0, maxResults: 1, total: 2, comments: [{ id: "1" }] },
        1: { startAt: 1, maxResults: 1, total: 2, comments: [{ id: "1" }] },
      },
      options: {},
    },
    {
      name: "total_changed",
      pages: {
        0: { startAt: 0, maxResults: 1, total: 2, comments: [{ id: "1" }] },
        1: { startAt: 1, maxResults: 1, total: 3, comments: [{ id: "2" }] },
      },
      options: {},
    },
    {
      name: "max_pages_exceeded",
      pages: {
        0: { startAt: 0, maxResults: 1, total: 2, comments: [{ id: "1" }] },
      },
      options: { maxPages: 1 },
    },
  ];
  for (const fixture of fixtures) {
    const result = await collectComments(
      new ExtractTransport({
        issues: {},
        commentPages: { "LEL-42": fixture.pages },
      }),
      "LEL-42",
      fixture.options
    );
    assert.equal(result.completeness.status, "incomplete", fixture.name);
    assert.equal(result.completeness.reason, fixture.name, fixture.name);
  }
});

test("comment pagination rejects nullable, wrong-type, and contradictory metadata", async () => {
  const malformedPages = [
    { startAt: null, maxResults: 50, total: 0, comments: [] },
    { startAt: 0, maxResults: null, total: 0, comments: [] },
    { startAt: 0, maxResults: 50, total: null, comments: [] },
    { startAt: 0, maxResults: 50, total: "", comments: [] },
    { startAt: "0", maxResults: 50, total: 0, comments: [] },
    { startAt: 0, maxResults: 50, comments: [] },
    {
      startAt: 0,
      maxResults: 1,
      total: 2,
      comments: [{ id: "1" }, { id: "2" }],
    },
    { startAt: 0, maxResults: 50, total: 0, comments: [{ id: "1" }] },
  ];
  for (const page of malformedPages) {
    await assert.rejects(
      collectComments(
        new ExtractTransport({
          issues: {},
          commentPages: { "LEL-42": { 0: page } },
        }),
        "LEL-42"
      ),
      JiraProtocolError
    );
  }
});

test("issue resource rejects key mismatch and a foreign instance", async () => {
  const mismatch = new ExtractTransport({
    issues: { "LEL-42": issue("LEL-43", 43) },
  });
  await assert.rejects(getIssueResource(mismatch, "LEL-42"), /requested key/);

  const foreign = issue("LEL-42", 42);
  foreign.self = "https://jira.invalid/rest/api/2/issue/42";
  await assert.rejects(
    getIssueResource(
      new ExtractTransport({ issues: { "LEL-42": foreign } }),
      "LEL-42"
    ),
    /another Jira instance/
  );
  await assert.rejects(
    getIssueResource(new ExtractTransport({ issues: {} }), "ABC-42"),
    /LEL issue key/
  );
});

test("malformed attachment and issue-link entries stay visible as gaps", async () => {
  const result = await extractIssue(
    new ExtractTransport({
      issues: {
        "LEL-42": issue("LEL-42", 42, {
          attachment: [{ id: "not-numeric", filename: "bad" }],
          issuelinks: [{ id: "1", type: {} }],
        }),
      },
      commentPages: { "LEL-42": { 0: emptyComments } },
    }),
    "LEL-42"
  );
  assert.equal(result.completeness.categories.attachments.status, "incomplete");
  assert.equal(result.completeness.categories.issueLinks.status, "incomplete");
  assert.equal(result.completeness.comprehensive, false);
});
