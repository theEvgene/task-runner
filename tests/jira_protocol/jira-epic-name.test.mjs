import assert from "node:assert/strict";
import test from "node:test";
import { createEpic } from "../../scripts/jira_protocol/operations.mjs";

class FakeTransport {
  constructor() {
    this.calls = [];
    this.quickModel = {
      atl_token: "model-csrf-token",
      formToken: "opaque-form-token",
      fields: [
        {
          id: "project",
          label: "Project",
          required: true,
          editHtml: '<input name="pid" value="10848">',
        },
        {
          id: "issuetype",
          label: "Issue type",
          required: true,
          editHtml: '<input name="issuetype" value="10000">',
        },
        {
          id: "summary",
          label: "Summary",
          required: true,
          editHtml: '<input name="summary" value="">',
        },
        {
          id: "customfield_10104",
          label: "Epic Name",
          required: false,
          editHtml: '<input name="customfield_10104" value="">',
        },
        {
          id: "customfield_10481",
          label: "Team",
          required: true,
          editHtml:
            '<select name="customfield_10481"><option value=""></option><option value="SERVICE">SERVICE</option></select>',
        },
      ],
    };
    this.issueState = null;
    this.board = {
      rapidViewId: 298290,
      columnsData: {
        columns: [{ id: 5934576, name: "To Do", statusIds: ["10206"] }],
      },
      issuesData: {
        issues: [{ key: "LEL-43", id: 43, statusId: "10206" }],
      },
    };
  }

  async request(request) {
    this.calls.push(request);
    if (request.path === "/secure/QuickCreateIssue!default.jspa")
      return { payload: this.quickModel };
    if (request.path === "/secure/QuickCreateIssue.jspa") {
      this.createdForm = new URLSearchParams(request.form);
      this.issueState = {
        key: "LEL-43",
        id: "43",
        fields: {
          project: { id: "10848", key: "LEL" },
          issuetype: { id: "10000", name: "Epic" },
          summary: this.createdForm.get("summary"),
          customfield_10104: this.createdForm.get("customfield_10104"),
          customfield_10481: {
            value: this.createdForm.get("customfield_10481"),
          },
        },
      };
      return { payload: { issueKey: "LEL-43" } };
    }
    if (request.path.startsWith("/rest/api/2/issue/"))
      return { payload: structuredClone(this.issueState) };
    if (request.path.endsWith("/work/allData.json"))
      return { payload: structuredClone(this.board) };
    if (request.path.endsWith("/issue/matchesBoard"))
      return {
        payload: { issueIdsOrKeys: ["LEL-43"], issuesInBacklog: ["LEL-43"] },
      };
    throw new Error(`unexpected request ${request.path}`);
  }
}

test("Epic Name defaults to summary when model marks it optional", async () => {
  const transport = new FakeTransport();
  const preview = await createEpic(transport, {
    summary: "New Epic",
    fields: { customfield_10481: "SERVICE" },
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.fields.customfield_10104, "New Epic");
  assert.equal(preview.operation.fields.summary, "New Epic");

  const result = await createEpic(transport, {
    summary: "New Epic",
    fields: { customfield_10481: "SERVICE" },
    confirm: preview.confirmation,
  });
  assert.equal(result.state, "Completed");
  assert.equal(result.issue.key, "LEL-43");
  assert.equal(result.readback.onBoard, true);
  assert.equal(transport.createdForm.get("customfield_10104"), "New Epic");
  assert.equal(transport.createdForm.get("summary"), "New Epic");
  assert.equal(
    result.readback.persistedFields.customfield_10104.expected,
    "New Epic"
  );
  assert.equal(
    result.readback.persistedFields.customfield_10104.actual,
    "New Epic"
  );
  assert.equal(result.readback.persistedFields.customfield_10104.matched, true);
});
