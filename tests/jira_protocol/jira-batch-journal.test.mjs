import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  BATCH_JOURNAL_SCHEMA,
  batchIdentity,
  createSubtaskBatch,
  normalizeSummary,
  parseResolveUnknown,
  planSubtaskBatch,
} from "../../scripts/jira_protocol/batch.mjs";
import {
  HANDOFF_SCHEMA,
  assertHandoffClean,
  toHandoffEvidence,
} from "../../scripts/jira_protocol/handoff.mjs";
import { createSubtask } from "../../scripts/jira_protocol/operations.mjs";
import {
  JiraProtocolError,
} from "../../scripts/jira_protocol/protocol.mjs";
import { planTransitionRoute } from "../../scripts/jira_protocol/route.mjs";

function createmeta() {
  return {
    projects: [
      {
        key: "LEL",
        id: "10848",
        issuetypes: [
          { id: "10100", name: "Task", subtask: false, fields: {} },
          {
            id: "10101",
            name: "Sub-task",
            subtask: true,
            fields: {
              summary: { name: "Summary", required: true },
              parent: { name: "Parent", required: true },
              description: { name: "Description", required: false },
            },
          },
        ],
      },
    ],
  };
}

class JournalFake {
  constructor({ children = [], deadReads = [], deny = null } = {}) {
    this.children = children.map((e) => ({ ...e }));
    this.deadReads = new Set(deadReads);
    this.deny = deny;
    this.posts = [];
    this.counter = 100;
    this.created = new Map();
  }
  async request(request) {
    if (request.path === "/rest/api/2/issue/createmeta")
      return { payload: structuredClone(createmeta()) };
    if (request.method === "POST" && request.path === "/rest/api/2/issue") {
      const fields = structuredClone(request.json?.fields ?? {});
      this.posts.push(fields);
      if (this.deny) throw this.deny;
      this.counter += 1;
      const key = `LEL-${this.counter}`;
      this.created.set(key, fields);
      return { payload: { key, id: String(this.counter) }, response: { status: 201 } };
    }
    if (request.path === "/rest/api/2/issue/LEL-42")
      return {
        payload: {
          key: "LEL-42",
          id: "42",
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10100", name: "Task" },
            subtasks: structuredClone(this.children),
          },
        },
        response: { status: 200 },
      };
    const m = /^\/rest\/api\/2\/issue\/(LEL-\d+)$/.exec(request.path ?? "");
    if (m) {
      const key = m[1];
      if (this.deadReads.has(key)) throw new JiraProtocolError("readback offline");
      const fields = this.created.get(key);
      if (!fields) throw new Error(`unknown key ${key}`);
      return {
        payload: {
          key,
          id: key.slice(4),
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10101", name: "Sub-task" },
            parent: { key: "LEL-42", id: "42" },
            summary: fields.summary,
            ...(fields.description !== undefined ? { description: fields.description } : {}),
          },
        },
        response: { status: 200 },
      };
    }
    throw new Error(`unexpected ${request.method ?? "GET"} ${request.path}`);
  }
}

function items(summaries) {
  return summaries.map((s) => ({ summary: s, normalized: normalizeSummary(s) }));
}

function tmpJournal() {
  const dir = mkdtempSync(join(tmpdir(), "batch-journal-"));
  return join(dir, "journal.json");
}

test("normalization is NFC, whitespace-collapsed, and case-sensitive", () => {
  assert.equal(normalizeSummary("  Foo\n\tBar  "), "Foo Bar");
  assert.equal(normalizeSummary("Foo\u00A0Bar"), "Foo Bar");
  assert.notEqual(normalizeSummary("Fix"), normalizeSummary("fix"));
  assert.equal(normalizeSummary("e\u0301"), "é");
  assert.equal(normalizeSummary("   "), "");
});

test("batch identity is stable and bound to inputs", () => {
  const a = batchIdentity({ parent: "lel-42", items: items(["A"]), allowDuplicates: [] });
  const b = batchIdentity({ parent: "LEL-42", items: items(["A"]), allowDuplicates: [] });
  const c = batchIdentity({ parent: "LEL-42", items: items(["B"]), allowDuplicates: [] });
  assert.equal(a, b);
  assert.notEqual(a, c);
});

test("journal persists each item and resumes after interruption", async () => {
  const path = tmpJournal();
  const transport = new JournalFake({ children: [] });
  const planned = items(["One", "Two", "Three"]);
  const preview = await createSubtaskBatch(transport, { parent: "LEL-42", items: planned, journalPath: path });
  assert.equal(preview.state, "AwaitingConfirmation");
  // No journal file on plan-only.
  assert.throws(() => readFileSync(path, "utf8"), /ENOENT/);
  const done = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview.confirmation,
    journalPath: path,
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.writes, 3);
  const stored = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(stored.schema, BATCH_JOURNAL_SCHEMA);
  assert.equal(stored.batchId, done.journal.batchId);
  assert.equal(Object.keys(stored.results).length, 3);
  // Simulate resume with a fresh transport that sees created children live:
  // journaled successes must not trigger new writes for already-done indexes.
  const resumeTransport = new JournalFake({ children: [] });
  const preview2 = await createSubtaskBatch(resumeTransport, {
    parent: "LEL-42",
    items: planned,
    journalPath: path,
  });
  // Plan still performs only reads.
  assert.equal(resumeTransport.posts.length, 0);
  const done2 = await createSubtaskBatch(resumeTransport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview2.confirmation,
    journalPath: path,
  });
  assert.equal(done2.state, "Completed");
  assert.equal(done2.writes, 0);
  assert.equal(resumeTransport.posts.length, 0);
  assert.equal(assertHandoffClean(done2), true);
});

test("journaled UnknownOutcome blocks rerun until explicit resolution", async () => {
  const path = tmpJournal();
  const transport = new JournalFake({ children: [] });
  const planned = items(["Solo"]);
  const preview = await createSubtaskBatch(transport, { parent: "LEL-42", items: planned });
  transport.deadReads.add("LEL-101");
  const first = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview.confirmation,
    journalPath: path,
  });
  assert.equal(first.state, "UnknownOutcome");
  const stored = JSON.parse(readFileSync(path, "utf8"));
  assert.ok(stored.unknown["1"]);
  const postsBefore = transport.posts.length;
  // Blind rerun with same journal must refuse before any new write.
  const transport2 = new JournalFake({ children: [] });
  // Point the second transport at the same journal path: plan reads only.
  const preview2 = await createSubtaskBatch(transport2, {
    parent: "LEL-42",
    items: planned,
    journalPath: path,
  });
  assert.equal(transport2.posts.length, 0);
  await assert.rejects(
    createSubtaskBatch(transport2, {
      parent: "LEL-42",
      items: planned,
      confirm: preview2.confirmation,
      journalPath: path,
    }),
    /UnknownOutcome.*resolve/i
  );
  assert.equal(transport2.posts.length, 0);
  assert.equal(transport.posts.length, postsBefore);
  // Explicit retry resolution unblocks exactly that index.
  const preview3 = await createSubtaskBatch(transport2, {
    parent: "LEL-42",
    items: planned,
    journalPath: path,
    resolveUnknown: ["1=retry"],
  });
  const done3 = await createSubtaskBatch(transport2, {
    parent: "LEL-42",
    items: planned,
    confirm: preview3.confirmation,
    journalPath: path,
    resolveUnknown: ["1=retry"],
  });
  assert.equal(done3.state, "Completed");
  assert.equal(transport2.posts.length, 1);
  assert.equal(parseResolveUnknown(["1=retry"]).get(1).mode, "retry");
  assert.equal(parseResolveUnknown(["2=LEL-105"]).get(2).key, "LEL-105");
});

test("401 preserves partial results and journal, with no blind retry", async () => {
  const path = tmpJournal();
  const transport = new JournalFake({ children: [] });
  const planned = items(["A", "B"]);
  const preview = await planSubtaskBatch(transport, { parent: "LEL-42", items: planned });
  // Fail the second write with 401.
  let calls = 0;
  const real = transport.request.bind(transport);
  transport.request = async (req) => {
    if (req.method === "POST" && req.path === "/rest/api/2/issue") {
      calls += 1;
      if (calls === 2) throw new JiraProtocolError("Jira returned HTTP 401", { status: 401 });
    }
    return real(req);
  };
  await assert.rejects(
    createSubtaskBatch(transport, {
      parent: "LEL-42",
      items: planned,
      confirm: preview.confirmation,
      journalPath: path,
    }),
    (error) => {
      assert.equal(error.status, 401);
      assert.ok(Array.isArray(error.details.partialResults));
      assert.equal(error.details.partialResults.length, 1);
      assert.equal(error.details.partialResults[0].state, "Created");
      return true;
    }
  );
  const stored = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(Object.keys(stored.results).length, 1);
});

test("secret detection catches prefixed keys and secret paths", () => {
  assert.throws(() => assertHandoffClean({ my_cookie: "raw" }), /not redacted/);
  assert.throws(() => assertHandoffClean({ "X-Auth-Token": "raw" }), /not redacted/);
  assert.throws(() => assertHandoffClean({ nested: { session_id: "raw" } }), /not redacted/);
  assert.doesNotThrow(() => assertHandoffClean({ my_cookie: "[REDACTED]" }));
  // Query and fragment are stripped, so a query token never reaches the path.
  const stripped = toHandoffEvidence({ stage: "s", method: "GET", path: "/rest/api/2/issue?token=abc#frag" });
  assert.equal(stripped.path, "/rest/api/2/issue");
  // A path carrying secret material fails.
  assert.throws(
    () => toHandoffEvidence({ stage: "s", method: "GET", path: "/x/session=abc" }),
    JiraProtocolError
  );
  assert.throws(
    () => toHandoffEvidence({ stage: "s", method: "GET", path: "/x/session-token" }),
    /secret/
  );
});

test("single subtask emits observed stages with separate readback", async () => {
  const t = new JournalFake({ children: [] });
  const preview = await createSubtask(t, { parent: "LEL-42", summary: "Proof" });
  assert.equal(preview.state, "AwaitingConfirmation");
  const done = await createSubtask(t, { parent: "LEL-42", summary: "Proof", confirm: preview.confirmation });
  assert.equal(done.state, "Completed");
  const stages = done.handoff.map((e) => e.stage);
  assert.deepEqual(stages, ["subtask_meta", "subtask_parent", "subtask_submission", "subtask_readback"]);
  for (const e of done.handoff) {
    assert.equal(e.schema, HANDOFF_SCHEMA);
    assert.ok(["GET", "POST"].includes(e.method));
  }
  const submission = done.handoff.find((e) => e.stage === "subtask_submission");
  const readback = done.handoff.find((e) => e.stage === "subtask_readback");
  assert.notEqual(submission.path, readback.path);
  assert.equal(submission.httpStatus, 201);
  assert.equal(readback.httpStatus, 200);
  assert.equal(assertHandoffClean(done.handoff), true);
});

test("route plan records actual observed discovery operations", async () => {
  const BOARD = {
    rapidViewId: 298290,
    columnsData: { columns: [{ id: 1, name: "To Do", statusIds: ["10206"] }, { id: 2, name: "Dev", statusIds: ["10217"] }] },
    issuesData: { issues: [] },
  };
  const transport = {
    origin: "https://jira3.tcsbank.ru",
    async request(req) {
      if (req.path.endsWith("/issue/details.json"))
        return { payload: { key: "LEL-42", id: 42, isSubtask: false }, response: { status: 200 } };
      if (req.path.endsWith("/issue/transitions.json"))
        return { payload: { issueId: 42, transitions: [{ id: 41, name: "Go", targetStatus: "10217", hasTransitionView: false }] }, response: { status: 200 } };
      if (req.path.endsWith("/work/allData.json")) return { payload: structuredClone(BOARD), response: { status: 200 } };
      if (req.path === "/rest/api/2/issue/LEL-42")
        return { payload: { key: "LEL-42", id: "42", fields: { status: { id: "10206", name: "To Do" }, project: { id: "10848", key: "LEL" }, issuetype: { id: "10100", name: "Task" } } }, response: { status: 200 } };
      throw new Error(`unexpected ${req.path}`);
    },
  };
  const plan = await planTransitionRoute(transport, { issueKey: "LEL-42", status: "10217" });
  assert.equal(plan.state, "AwaitingConfirmation");
  const stages = plan.handoff.map((e) => e.stage);
  assert.ok(stages.includes("route_state"));
  assert.ok(stages.includes("route_transitions"));
  assert.ok(stages.includes("route_board"));
  for (const e of plan.handoff) assert.equal(e.httpStatus, 200);
  assert.equal(assertHandoffClean(plan.handoff), true);
});
