import test from "node:test";
import assert from "node:assert/strict";
import { flagIssue } from "../../scripts/jira_protocol/operations.mjs";
import { JiraAmbiguousOutcome } from "../../scripts/jira_protocol/protocol.mjs";
import {
  assertSingleMutation,
  createSequencedTransport,
} from "./helpers/ambiguous-write.mjs";

function flagPrefix(action) {
  return action === "add" ? "(flag) Flag added" : "(flagoff) Flag removed";
}

function flagState(action, written) {
  const flagged = written === (action === "add");
  return flagged ? [{ value: "Impediment" }] : null;
}

function issueState(key, flagValue) {
  return { response: { payload: { key, fields: { customfield_10641: flagValue } } } };
}

function commentsState(list) {
  return {
    response: {
      payload: {
        startAt: 0,
        total: list.length,
        comments: list,
      },
    },
  };
}

function flagSteps(action, { afterComments }) {
  const before = [{ id: "1", body: "old" }];
  return [
    issueState("LEL-1", flagState(action, false)),
    commentsState(before),
    { response: { payload: '<input name="formToken" value="secret">' } },
    { response: { payload: {} } },
    issueState("LEL-1", flagState(action, true)),
    commentsState(afterComments),
  ];
}

function flagBody(action) {
  return `${flagPrefix(action)}\n\nA & B`;
}

for (const action of ["add", "remove"])
  test(`flag ${action} requires confirmation and verifies a new comment`, async () => {
    const afterComments = [
      { id: "1", body: "old" },
      { id: "2", body: flagBody(action) },
    ];
    const steps = [
      ...flagSteps(action, { afterComments }),
      issueState("LEL-1", flagState(action, true)),
    ];
    const { transport, calls, mutationCount, requestOrder } =
      createSequencedTransport(steps);
    const input = { issueKey: "LEL-1", action, comment: " A & B " };
    const plan = await flagIssue(transport, input);
    assert.equal(calls.length, 0);
    const result = await flagIssue(transport, {
      ...input,
      confirm: plan.confirmation,
    });
    assert.equal(result.readback.commentId, "2");
    const write = calls.find((c) => c.mutation);
    assert.equal(
      write.json.comment,
      `${flagPrefix(action)}\n\nA &amp; B`
    );
    assert.equal(write.json.formToken, "secret");
    await assert.rejects(
      flagIssue(transport, { ...input, confirm: plan.confirmation }),
      /already/
    );
    assert.equal(mutationCount(), 1);
    assertSingleMutation(calls);
    assert.deepEqual(requestOrder().slice(0, 4), [
      "GET /rest/api/2/issue/LEL-1",
      "GET /rest/api/2/issue/LEL-1/comment",
      "GET /secure/GHBulkCommentDialog.jspa",
      "POST /rest/greenhopper/1.0/xboard/issue/flag/flag.json",
    ]);
  });

test("flag rejects blank comments", async () => {
  await assert.rejects(
    flagIssue({}, { issueKey: "LEL-1", action: "add", comment: " " }),
    /required/
  );
});

test("flag reports an ambiguous outcome when the new comment is absent", async () => {
  const { transport, calls, mutationCount } = createSequencedTransport(
    flagSteps("add", { afterComments: [] })
  );
  const input = { issueKey: "LEL-1", action: "add", comment: "reason" };
  const plan = await flagIssue(transport, input);
  await assert.rejects(
    flagIssue(transport, { ...input, confirm: plan.confirmation }),
    { name: "JiraAmbiguousOutcome" }
  );
  assert.equal(mutationCount(), 1);
  assertSingleMutation(calls);
});

function unreadableWriteTransport({
  flagAfter = true,
  commentsAfter = true,
} = {}) {
  const calls = [];
  const transport = {
    calls,
    async request(request) {
      calls.push(request);
      if (request.path.endsWith("/comment")) {
        if (calls.filter((c) => c.mutation).length === 0)
          return {
            payload: {
              startAt: 0,
              total: 1,
              comments: [{ id: "1", body: "old" }],
            },
          };
        if (!commentsAfter)
          return {
            payload: {
              startAt: 0,
              total: 1,
              comments: [{ id: "1", body: "old" }],
            },
          };
        return {
          payload: {
            startAt: 0,
            total: 2,
            comments: [
              { id: "1", body: "old" },
              { id: "2", body: "(flag) Flag added\n\nreason" },
            ],
          },
        };
      }
      if (request.path.includes("GHBulk"))
        return { payload: '<input name="formToken" value="secret">' };
      if (request.mutation)
        throw new JiraAmbiguousOutcome(
          "Jira returned an unreadable success response after a write may have completed"
        );
      const applied = calls.some((c) => c.mutation);
      return {
        payload: {
          key: "LEL-1",
          fields: {
            customfield_10641:
              applied && flagAfter ? [{ value: "Impediment" }] : null,
          },
        },
      };
    },
  };
  return transport;
}

test("flag recovers with one safe readback after an unreadable successful write", async () => {
  const transport = unreadableWriteTransport({
    flagAfter: true,
    commentsAfter: true,
  });
  const input = { issueKey: "LEL-1", action: "add", comment: "reason" };
  const plan = await flagIssue(transport, input);
  const result = await flagIssue(transport, {
    ...input,
    confirm: plan.confirmation,
  });
  assert.equal(result.state, "Completed");
  assert.equal(result.readback.commentId, "2");
  assert.equal(result.readback.flag, true);
  assert.equal(transport.calls.filter((c) => c.mutation).length, 1);
});

test("flag stays ambiguous after unreadable write without a new comment", async () => {
  const transport = unreadableWriteTransport({
    flagAfter: true,
    commentsAfter: false,
  });
  const input = { issueKey: "LEL-1", action: "add", comment: "reason" };
  const plan = await flagIssue(transport, input);
  await assert.rejects(
    flagIssue(transport, { ...input, confirm: plan.confirmation }),
    {
      name: "JiraAmbiguousOutcome",
    }
  );
  assert.equal(transport.calls.filter((c) => c.mutation).length, 1);
});

test("flag stays ambiguous after unreadable write with a wrong flag state", async () => {
  const transport = unreadableWriteTransport({
    flagAfter: false,
    commentsAfter: true,
  });
  const input = { issueKey: "LEL-1", action: "add", comment: "reason" };
  const plan = await flagIssue(transport, input);
  await assert.rejects(
    flagIssue(transport, { ...input, confirm: plan.confirmation }),
    {
      name: "JiraAmbiguousOutcome",
    }
  );
  assert.equal(transport.calls.filter((c) => c.mutation).length, 1);
});

test("flag stays ambiguous on failed or ambiguous comment readback", async () => {
  for (const failure of ["read", "duplicate"]) {
    const transport = unreadableWriteTransport();
    const request = transport.request.bind(transport);
    transport.request = async (call) => {
      if (
        call.path.endsWith("/comment") &&
        transport.calls.some((c) => c.mutation)
      ) {
        if (failure === "read") throw new Error("read unavailable");
        return {
          payload: {
            startAt: 0,
            total: 3,
            comments: [
              { id: "1", body: "old" },
              { id: "2", body: "(flag) Flag added\n\nreason" },
              { id: "3", body: "(flag) Flag added\n\nreason" },
            ],
          },
        };
      }
      return request(call);
    };
    const input = { issueKey: "LEL-1", action: "add", comment: "reason" };
    const plan = await flagIssue(transport, input);
    await assert.rejects(
      flagIssue(transport, { ...input, confirm: plan.confirmation }),
      { name: "JiraAmbiguousOutcome" }
    );
    assert.equal(transport.calls.filter((c) => c.mutation).length, 1);
  }
});
