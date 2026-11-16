import test from "node:test";
import assert from "node:assert/strict";
import { flagIssue } from "../../scripts/jira_protocol/operations.mjs";

for (const action of ["add", "remove"])
  test(`flag ${action} requires confirmation and verifies a new comment`, async () => {
    const calls = [];
    let written = false;
    const transport = {
      async request(request) {
        calls.push(request);
        if (request.path.endsWith("/comment"))
          return {
            payload: {
              startAt: 0,
              total: written ? 2 : 1,
              comments: [
                { id: "1", body: "old" },
                ...(written
                  ? [
                      {
                        id: "2",
                        body: `${action === "add" ? "(flag) Flag added" : "(flagoff) Flag removed"}\n\nA & B`,
                      },
                    ]
                  : []),
              ],
            },
          };
        if (request.path.includes("GHBulk"))
          return { payload: '<input name="formToken" value="secret">' };
        if (request.mutation) {
          written = true;
          return { payload: {} };
        }
        return {
          payload: {
            key: "LEL-1",
            fields: {
              customfield_10641:
                written === (action === "add")
                  ? [{ value: "Impediment" }]
                  : null,
            },
          },
        };
      },
    };
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
      `${action === "add" ? "(flag) Flag added" : "(flagoff) Flag removed"}\n\nA &amp; B`
    );
    assert.equal(write.json.formToken, "secret");
    await assert.rejects(
      flagIssue(transport, { ...input, confirm: plan.confirmation }),
      /already/
    );
    assert.equal(calls.filter((c) => c.mutation).length, 1);
  });

test("flag rejects blank comments", async () => {
  await assert.rejects(
    flagIssue({}, { issueKey: "LEL-1", action: "add", comment: " " }),
    /required/
  );
});

test("flag reports an ambiguous outcome when the new comment is absent", async () => {
  let written = false;
  const transport = {
    async request(request) {
      if (request.path.endsWith("/comment"))
        return { payload: { startAt: 0, total: 0, comments: [] } };
      if (request.path.includes("GHBulk"))
        return { payload: '<input name="formToken" value="secret">' };
      if (request.mutation) {
        written = true;
        return { payload: {} };
      }
      return {
        payload: {
          key: "LEL-1",
          fields: {
            customfield_10641: written ? [{ value: "Impediment" }] : null,
          },
        },
      };
    },
  };
  const input = { issueKey: "LEL-1", action: "add", comment: "reason" };
  const plan = await flagIssue(transport, input);
  await assert.rejects(
    flagIssue(transport, { ...input, confirm: plan.confirmation }),
    { name: "JiraAmbiguousOutcome" }
  );
});
