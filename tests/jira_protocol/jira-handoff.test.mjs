import assert from "node:assert/strict";
import test from "node:test";
import { serializeReceipt } from "../../scripts/jira-protocol.mjs";
import { createSubtask } from "../../scripts/jira_protocol/operations.mjs";
import {
  HANDOFF_SCHEMA,
  assertHandoffClean,
  handoffStep,
  toHandoffEvidence,
} from "../../scripts/jira_protocol/handoff.mjs";
import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
} from "../../scripts/jira_protocol/protocol.mjs";
import { errorReceipt } from "../../scripts/jira-protocol.mjs";

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

function parentState() {
  return {
    key: "LEL-42",
    id: "42",
    fields: {
      project: { id: "10848", key: "LEL" },
      issuetype: { id: "10100", name: "Task" },
    },
  };
}

class HandoffFake {
  constructor({ denySubmissionWith, breakReadback = false } = {}) {
    this.posts = 0;
    this.denySubmissionWith = denySubmissionWith;
    this.breakReadback = breakReadback;
    this.createdState = null;
  }

  async request(request) {
    if (request.path === "/rest/api/2/issue/createmeta")
      return { payload: structuredClone(createmeta()) };
    if (request.method === "POST" && request.path === "/rest/api/2/issue") {
      this.posts += 1;
      if (this.denySubmissionWith) throw this.denySubmissionWith;
      const fields = request.json?.fields ?? {};
      this.createdState = {
        key: "LEL-99",
        id: "99",
        fields: {
          project: { id: "10848", key: "LEL" },
          issuetype: { id: "10101", name: "Sub-task" },
          parent: { key: "LEL-42", id: "42" },
          summary: fields.summary,
          description: fields.description ?? null,
        },
      };
      return { payload: { key: "LEL-99", id: "99" } };
    }
    if (request.path === "/rest/api/2/issue/LEL-42")
      return { payload: structuredClone(parentState()) };
    if (request.path === "/rest/api/2/issue/LEL-99") {
      if (this.breakReadback)
        throw new JiraProtocolError("fixture readback offline");
      return { payload: structuredClone(this.createdState) };
    }
    throw new Error(`unexpected request ${request.path}`);
  }
}

test("ledger steps keep observed, inferred, and assumed apart", () => {
  const observed = handoffStep({
    step: "read board",
    stage: "route_plan",
    category: "observed",
    source: "sanitized capture 2026-11-14",
    verification: "payload matched the requested board",
    gaps: [],
  });
  assert.equal(observed.category, "observed");
  assert.ok(Object.isFrozen(observed));
  for (const category of ["observed", "inferred", "assumed"]) {
    const entry = handoffStep({
      step: "s",
      stage: "t",
      category,
      source: "src",
      verification: null,
      gaps: ["not yet proven live"],
    });
    assert.equal(entry.category, category);
  }
  assert.throws(
    () =>
      handoffStep({
        step: "s",
        stage: "t",
        category: "proven",
        source: "src",
        verification: null,
        gaps: ["g"],
      }),
    /category must be one of/
  );
});

test("unverified steps must list their gaps instead of claiming proof", () => {
  assert.throws(
    () =>
      handoffStep({
        step: "submit",
        stage: "subtask_submission",
        category: "assumed",
        source: "guess",
        verification: null,
        gaps: [],
      }),
    /must list its evidence gaps/
  );
  const entry = handoffStep({
    step: "submit",
    stage: "subtask_submission",
    category: "assumed",
    source: "guess",
    verification: null,
    gaps: ["live route unverified"],
  });
  assert.deepEqual(entry.gaps, ["live route unverified"]);
});

test("handoff evidence strips query and fragment and never invents status", () => {
  const evidence = toHandoffEvidence({
    stage: "subtask_submission",
    method: "post",
    path: "/rest/api/2/issue?os_authType=basic#frag",
  });
  assert.deepEqual(evidence, {
    schema: HANDOFF_SCHEMA,
    stage: "subtask_submission",
    method: "POST",
    path: "/rest/api/2/issue",
    httpStatus: "unknown",
  });
  const known = toHandoffEvidence({
    stage: "subtask_meta",
    method: "GET",
    path: "/rest/api/2/issue/createmeta",
    httpStatus: 403,
  });
  assert.equal(known.httpStatus, 403);
  for (const bad of [
    { stage: "Bad Stage", method: "GET", path: "/x" },
    { stage: "ok_stage", method: "DELETE", path: "/x" },
    { stage: "ok_stage", method: "GET", path: "https://evil.invalid/x" },
    { stage: "ok_stage", method: "GET", path: "/x y" },
    { stage: "ok_stage", method: "GET", path: "/x", httpStatus: 99 },
  ])
    assert.throws(() => toHandoffEvidence(bad), JiraProtocolError);
});

test("sanitized evidence rejects secret material and foreign keys", () => {
  assert.equal(
    assertHandoffClean(
      toHandoffEvidence({ stage: "s", method: "GET", path: "/x" })
    ),
    true
  );
  assert.throws(
    () => assertHandoffClean({ cookie: "raw-cookie-value" }),
    /not redacted/
  );
  assert.throws(
    () => assertHandoffClean({ nested: { atl_token: "raw-token" } }),
    /not redacted/
  );
  assert.throws(
    () =>
      assertHandoffClean({
        schema: HANDOFF_SCHEMA,
        stage: "s",
        method: "GET",
        path: "/x",
        httpStatus: "unknown",
        body: "must not ride along",
      }),
    /unexpected key body/
  );
  assert.doesNotThrow(() =>
    assertHandoffClean({ atl_token: "[REDACTED]", formToken: "[REDACTED]" })
  );
});

test("fixture proves the plan, write, and readback path with clean evidence", async () => {
  const transport = new HandoffFake();
  const input = {
    parent: "LEL-42",
    summary: "Handoff proof",
    description: "Details",
  };
  const preview = await createSubtask(transport, input);
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(transport.posts, 0);
  const done = await createSubtask(transport, {
    ...input,
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.issue.key, "LEL-99");
  assert.equal(transport.posts, 1);
  const evidence = [
    toHandoffEvidence({
      stage: "subtask_meta",
      method: "GET",
      path: "/rest/api/2/issue/createmeta",
    }),
    toHandoffEvidence({
      stage: "subtask_submission",
      method: "POST",
      path: "/rest/api/2/issue",
    }),
    toHandoffEvidence({
      stage: "batch_item_readback",
      method: "GET",
      path: "/rest/api/2/issue/LEL-99",
    }),
  ];
  assert.equal(assertHandoffClean({ receipt: done, evidence }), true);
  const serialized = serializeReceipt({ done, evidence });
  assert.doesNotMatch(serialized, /cookie/i);
  assert.doesNotMatch(serialized, /atl_token|formToken/);
});

test("ambiguous fixture claims no success and performs exactly one write", async () => {
  const transport = new HandoffFake({ breakReadback: true });
  const preview = await createSubtask(transport, {
    parent: "LEL-42",
    summary: "Handoff proof",
  });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Handoff proof",
      confirm: preview.confirmation,
    }),
    (error) => {
      assert.ok(error instanceof JiraAmbiguousOutcome);
      assert.equal(error.issueKey, "LEL-99");
      const receipt = errorReceipt(error);
      assert.equal(receipt.state, "UnknownOutcome");
      assert.equal(assertHandoffClean(receipt), true);
      return true;
    }
  );
  assert.equal(transport.posts, 1);
});

test("denied fixture keeps the 401/403 rule, stage, and no secret", async () => {
  const transport = new HandoffFake({
    denySubmissionWith: new JiraProtocolError("Jira returned HTTP 403", {
      status: 403,
    }),
  });
  const preview = await createSubtask(transport, {
    parent: "LEL-42",
    summary: "Handoff proof",
  });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Handoff proof",
      confirm: preview.confirmation,
    }),
    (error) => {
      assert.ok(error instanceof JiraProtocolError);
      const receipt = errorReceipt(error);
      assert.equal(receipt.state, "Failed");
      assert.equal(receipt.httpStatus, 403);
      assert.equal(receipt.stage, "subtask_submission");
      assert.equal(assertHandoffClean(receipt), true);
      return true;
    }
  );
  assert.equal(transport.posts, 1);
});

test("denied fixture keeps the 401 rule as well", async () => {
  const transport = new HandoffFake({
    denySubmissionWith: new JiraProtocolError("Jira returned HTTP 401", {
      status: 401,
    }),
  });
  const preview = await createSubtask(transport, {
    parent: "LEL-42",
    summary: "Handoff proof",
  });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Handoff proof",
      confirm: preview.confirmation,
    }),
    (error) => {
      const receipt = errorReceipt(error);
      assert.equal(receipt.state, "Failed");
      assert.equal(receipt.httpStatus, 401);
      assert.equal(receipt.stage, "subtask_submission");
      return true;
    }
  );
  assert.equal(transport.posts, 1);
});
