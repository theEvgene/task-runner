import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import {
  errorReceipt,
  serializeReceipt,
} from "../../scripts/jira-protocol.mjs";
import { defaultsFromEditHtml } from "../../scripts/jira_protocol/html-form.mjs";
import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  JiraTransport,
  redactSecrets,
} from "../../scripts/jira_protocol/protocol.mjs";
import {
  assignIssue,
  getIssue,
  getIssueState,
  getIssueTransitions,
  transitionIssue,
} from "../../scripts/jira_protocol/operations.mjs";
import { createUnreadableMutationTransport } from "./helpers/ambiguous-write.mjs";

function details(key = "LEL-42", overrides = {}) {
  return {
    key,
    id: key === "LEL-43" ? 43 : 42,
    isSubtask: false,
    editable: true,
    tabs: { defaultTabs: [] },
    ...overrides,
  };
}

function board(key = "LEL-42", issue = {}) {
  return {
    rapidViewId: 298290,
    columnsData: {
      columns: [
        { id: 5934576, name: "To Do", statusIds: ["10206", "10400"] },
        { id: 5934577, name: "Development", statusIds: ["10217"] },
      ],
    },
    issuesData: {
      issues: [
        { key, id: 42, statusId: "10206", assignee: "old.user", ...issue },
      ],
    },
  };
}

class FakeTransport {
  constructor() {
    this.calls = [];
    this.atlToken = "test-csrf-token";
    this.issue = details();
    this.issueState = {
      key: "LEL-42",
      id: "42",
      fields: {
        status: { id: "10206", name: "To Do" },
        assignee: { name: "old.user" },
        project: { id: "10848", key: "LEL" },
        issuetype: { id: "10100", name: "Task" },
      },
    };
    this.board = board();
    this.board.issuesData.issues = [
      { key: "LEL-42", id: 42, statusId: "10206", assignee: "old.user" },
      { key: "LEL-43", id: 43, statusId: "10206" },
    ];
    this.transitions = {
      issueId: 42,
      transitions: [
        {
          id: 41,
          name: "In Progress",
          targetStatus: 10217,
          hasTransitionView: false,
        },
      ],
    };
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
          id: "description",
          label: "Description",
          required: false,
          editHtml: '<textarea name="description"></textarea>',
        },
        {
          id: "customfield_10104",
          label: "Epic Name",
          required: true,
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
  }

  async request(request) {
    this.calls.push(request);
    if (request.path.endsWith("/issue/details.json"))
      return { payload: this.issue };
    if (request.path.startsWith("/rest/api/2/issue/"))
      return { payload: structuredClone(this.issueState) };
    if (request.path.endsWith("/issue/transitions.json"))
      return { payload: this.transitions };
    if (request.path.endsWith("/work/allData.json"))
      return { payload: structuredClone(this.board) };
    if (request.path.endsWith("/transitionAndRank")) {
      if (this.board.issuesData.issues[0])
        this.board.issuesData.issues[0].statusId = "10217";
      this.issueState.fields.status = { id: "10217", name: "Development" };
      return { payload: {} };
    }
    if (request.path === "/secure/AssignIssue.jspa") {
      if (this.board.issuesData.issues[0])
        this.board.issuesData.issues[0].assignee = request.query.assignee;
      this.issueState.fields.assignee = { name: request.query.assignee };
      return { payload: "assigned" };
    }
    if (request.path === "/secure/QuickCreateIssue!default.jspa")
      return { payload: this.quickModel };
    if (request.path === "/secure/QuickCreateIssue.jspa") {
      this.createdForm = new URLSearchParams(request.form);
      this.issue = details("LEL-43", { id: 43 });
      this.issueState = {
        key: "LEL-43",
        id: "43",
        fields: {
          status: { id: "10206", name: "To Do" },
          assignee: null,
          project: { id: "10848", key: "LEL" },
          issuetype: { id: "10000", name: "Epic" },
          summary: this.createdForm.get("summary"),
          description: this.createdForm.get("description"),
          customfield_10104: this.createdForm.get("customfield_10104"),
          customfield_10481: {
            value: this.createdForm.get("customfield_10481"),
          },
        },
      };
      return { payload: { issueKey: "LEL-43" } };
    }
    if (request.path.endsWith("/issue/matchesBoard"))
      return {
        payload: {
          issueIdsOrKeys: ["LEL-43"],
          issuesInBacklog: ["LEL-43"],
        },
      };
    throw new Error(`unexpected request ${request.path}`);
  }
}

test("detail-view retrieval binds the LEL board and preserves its hybrid body", async () => {
  const transport = new FakeTransport();
  transport.issue.extraServerField = { html: "<strong>kept</strong>" };
  const result = await getIssue(transport, "lel-42");
  assert.deepEqual(result.extraServerField, { html: "<strong>kept</strong>" });
  assert.deepEqual(transport.calls[0].query, {
    rapidViewId: 298290,
    issueIdOrKey: "LEL-42",
    loadSubtasks: true,
  });
});

test("issue-state readback is key-specific and requests only evidenced fields", async () => {
  const transport = new FakeTransport();
  const result = await getIssueState(transport, "lel-42");
  assert.equal(result.fields.status.id, "10206");
  assert.deepEqual(transport.calls[0], {
    path: "/rest/api/2/issue/LEL-42",
    query: { fields: "status,assignee,project,issuetype" },
  });
});

test("transition discovery resolves numeric issue id before querying", async () => {
  const transport = new FakeTransport();
  const result = await getIssueTransitions(transport, "LEL-42");
  assert.equal(result.transitions[0].id, 41);
  assert.equal(transport.calls[1].query.issueId, 42);
});

test("transition requires a digest and writes only the observed payload", async () => {
  const transport = new FakeTransport();
  const input = {
    issueKey: "LEL-42",
    transitionId: "41",
    targetColumn: "5934577",
  };
  const preview = await transitionIssue(transport, input);
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(
    transport.calls.some((call) => call.path.endsWith("transitionAndRank")),
    false
  );
  const completed = await transitionIssue(transport, {
    ...input,
    confirm: preview.confirmation,
  });
  assert.equal(completed.state, "Completed");
  const write = transport.calls.find((call) =>
    call.path.endsWith("transitionAndRank")
  );
  assert.deepEqual(write.json, {
    rapidViewId: 298290,
    issueKeys: ["LEL-42"],
    targetColumn: 5934577,
    selectedTransitionId: 41,
  });
  assert.equal(write.mutation, true);
  const readback = transport.calls.find((call) =>
    call.path.startsWith("/rest/api/2/issue/")
  );
  assert.equal(readback.query.fields, "status,assignee,project,issuetype");
});

test("transition confirmation binds displayed transition and column names", async () => {
  for (const renamed of ["transition", "column"]) {
    const transport = new FakeTransport();
    const input = {
      issueKey: "LEL-42",
      transitionId: 41,
      targetColumn: 5934577,
    };
    const preview = await transitionIssue(transport, input);
    if (renamed === "transition")
      transport.transitions.transitions[0].name = "Renamed transition";
    else transport.board.columnsData.columns[1].name = "Renamed column";
    const changed = await transitionIssue(transport, {
      ...input,
      confirm: preview.confirmation,
    });
    assert.equal(changed.state, "AwaitingConfirmation", renamed);
    assert.notEqual(changed.confirmation, preview.confirmation, renamed);
    assert.equal(
      changed.operation.transitionName,
      renamed === "transition" ? "Renamed transition" : "In Progress"
    );
    assert.equal(
      changed.operation.targetColumnName,
      renamed === "column" ? "Renamed column" : "Development"
    );
    assert.equal(
      transport.calls.some((call) => call.path.endsWith("transitionAndRank")),
      false,
      renamed
    );
  }
});

for (const fixture of [
  { label: "task", isSubtask: false, issueType: { id: "10100", name: "Task" } },
  {
    label: "subtask",
    isSubtask: true,
    issueType: { id: "10101", name: "Sub-task" },
  },
  { label: "Epic", isSubtask: false, issueType: { id: "10000", name: "Epic" } },
]) {
  test(`transition confirms ${fixture.label} absent from top-level board issues`, async () => {
    const transport = new FakeTransport();
    transport.issue.isSubtask = fixture.isSubtask;
    transport.issueState.fields.issuetype = fixture.issueType;
    transport.board.issuesData.issues = [];
    const input = {
      issueKey: "LEL-42",
      transitionId: 41,
      targetColumn: 5934577,
    };
    const preview = await transitionIssue(transport, input);
    const result = await transitionIssue(transport, {
      ...input,
      confirm: preview.confirmation,
    });
    assert.equal(result.state, "Completed");
    assert.equal(result.readback.statusId, "10217");
  });
}

test("transition refuses workflow forms instead of bypassing them", async () => {
  const transport = new FakeTransport();
  transport.transitions.transitions[0].hasTransitionView = true;
  await assert.rejects(
    transitionIssue(transport, {
      issueKey: "LEL-42",
      transitionId: 41,
      targetColumn: 5934577,
    }),
    /requires a workflow form/
  );
  assert.equal(
    transport.calls.some((call) => call.path.endsWith("transitionAndRank")),
    false
  );
});

test("assignment uses the captured action and confirms issue readback", async () => {
  const transport = new FakeTransport();
  const preview = await assignIssue(transport, {
    issueKey: "LEL-42",
    assignee: "new.user",
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  const result = await assignIssue(transport, {
    issueKey: "LEL-42",
    assignee: "new.user",
    confirm: preview.confirmation,
  });
  assert.equal(result.state, "Completed");
  const write = transport.calls.find(
    (call) => call.path === "/secure/AssignIssue.jspa"
  );
  assert.deepEqual(write.query, {
    atl_token: "test-csrf-token",
    id: 42,
    assignee: "new.user",
  });
  assert.equal(write.mutation, true);
});

test("assignment confirms an issue absent from top-level board issues", async () => {
  const transport = new FakeTransport();
  transport.board.issuesData.issues = [];
  const preview = await assignIssue(transport, {
    issueKey: "LEL-42",
    assignee: "new.user",
  });
  const result = await assignIssue(transport, {
    issueKey: "LEL-42",
    assignee: "new.user",
    confirm: preview.confirmation,
  });
  assert.equal(result.state, "Completed");
  assert.equal(result.readback.assignee, "new.user");
});


test("HTML defaults retain checked controls and selected options only", () => {
  const values = defaultsFromEditHtml(`
    <input name="hidden" value="x&amp;y">
    <input type="checkbox" name="unchecked" value="no">
    <input type="checkbox" name="checked" value="yes" checked>
    <select name="team"><option value="A">A</option><option value="B" selected>B</option></select>
  `);
  assert.deepEqual(Object.fromEntries(values), {
    hidden: ["x&y"],
    checked: ["yes"],
    team: ["B"],
  });
});

test("mutation transport failures are unknown outcomes and never expose cookie", async () => {
  const transport = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "secret-cookie",
    allowTestOrigin: true,
    fetchImpl: async () => {
      throw new Error("offline");
    },
  });
  await assert.rejects(
    transport.request({ path: "/write", method: "POST", mutation: true }),
    JiraAmbiguousOutcome
  );
});

test("mutation 2xx with an unreadable JSON body is an unknown outcome", async () => {
  const { transport, requests } = createUnreadableMutationTransport({
    status: 200,
    body: "",
  });
  await assert.rejects(
    transport.request({ path: "/write", method: "PUT", mutation: true }),
    JiraAmbiguousOutcome
  );
  assert.equal(requests.length, 1);
});

test("transport pins production credentials to Jira and explicit loopback test mode", () => {
  assert.throws(
    () =>
      new JiraTransport({
        origin: "http://example.invalid",
        cookie: "secret-cookie",
      }),
    /must be https:\/\/jira3\.tcsbank\.ru/
  );
  assert.throws(
    () =>
      new JiraTransport({
        origin: "https://example.invalid",
        cookie: "secret-cookie",
        allowTestOrigin: true,
      }),
    /loopback origins only/
  );
  assert.doesNotThrow(
    () =>
      new JiraTransport({
        origin: "http://localhost:9123",
        cookie: "fixture-cookie",
        allowTestOrigin: true,
      })
  );
});

test("CLI has no credential command-line options", () => {
  const result = spawnSync(
    process.execPath,
    [
      "scripts/jira-protocol.mjs",
      "detail-view",
      "--issue",
      "LEL-42",
      "--cookie",
      "secret",
    ],
    {
      cwd: process.cwd(),
      encoding: "utf8",
      env: { ...process.env, JIRA_SESSION_COOKIE: "fixture" },
    }
  );
  assert.equal(result.status, 2);
  assert.match(result.stdout, /unknown option --cookie/);
  assert.doesNotMatch(result.stdout, /secret/);
});

test("CLI rejects create-only field options before requiring a session", () => {
  const result = spawnSync(
    process.execPath,
    [
      "scripts/jira-protocol.mjs",
      "detail-view",
      "--issue",
      "LEL-42",
      "--field",
      "summary=x",
    ],
    { cwd: process.cwd(), encoding: "utf8", env: { ...process.env } }
  );
  assert.equal(result.status, 2);
  assert.match(result.stdout, /unknown option --field/);
  assert.doesNotMatch(result.stdout, /JIRA_SESSION_COOKIE/);
});

test("CLI does not expose the old misleading get command", () => {
  const result = spawnSync(
    process.execPath,
    ["scripts/jira-protocol.mjs", "get", "--issue", "LEL-42"],
    { cwd: process.cwd(), encoding: "utf8", env: { ...process.env } }
  );
  assert.equal(result.status, 2);
  assert.match(result.stdout, /unknown command get/);
  assert.doesNotMatch(result.stdout, /JIRA_SESSION_COOKIE/);
});

test("CLI output redacts token inputs independent of attribute syntax", () => {
  const result = redactSecrets({
    atl_token: "object-secret",
    action: "/secure/AssignIssue.jspa?id=42&atl_token=query-secret",
    html: '<input name="formToken" value="hidden-secret">',
    summary: "kept",
  });
  assert.deepEqual(result, {
    atl_token: "[REDACTED]",
    action: "/secure/AssignIssue.jspa?id=42&atl_token=[REDACTED]",
    html: '<input name="formToken" value="[REDACTED]">',
    summary: "kept",
  });

  for (const html of [
    '<input value="secret-a" name="atl_token">',
    "<INPUT VALUE='secret-b' ID='FORMTOKEN'>",
    "<input value=secret-c name=atl_token>",
    '<input class="x" VALUE=secret-d Id="Atl_Token">',
    '<input name="atl_token" data-value="public" value="secret-e">',
    '<input aria-value="public" value="secret-f" id="formToken">',
  ]) {
    const redacted = redactSecrets(html);
    assert.match(redacted, /\[REDACTED\]/);
    assert.doesNotMatch(redacted, /secret-[a-f]/);
  }

  const serialized = serializeReceipt({
    detailView: {
      html: '<input value="cli-boundary-secret" name="formToken">',
    },
  });
  assert.match(serialized, /\[REDACTED\]/);
  assert.doesNotMatch(serialized, /cli-boundary-secret/);
});

