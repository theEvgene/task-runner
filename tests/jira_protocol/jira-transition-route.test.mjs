import assert from "node:assert/strict";
import test from "node:test";
import { serializeReceipt } from "../../scripts/jira-protocol.mjs";
import { JiraProtocolError } from "../../scripts/jira_protocol/protocol.mjs";
import { assertHandoffClean } from "../../scripts/jira_protocol/handoff.mjs";
import {
  ROUTE_MAX_STEPS,
  planTransitionRoute,
  transitionToStatus,
} from "../../scripts/jira_protocol/route.mjs";

const BOARD = {
  rapidViewId: 298290,
  columnsData: {
    columns: [
      { id: 5934576, name: "To Do", statusIds: ["10206"] },
      { id: 5934577, name: "Development", statusIds: ["10217", "10218"] },
    ],
  },
  issuesData: { issues: [] },
};

const STATUS_NAMES = { 10206: "To Do", 10217: "In Progress", 10218: "Review" };

const STATUS_CATALOG = Object.entries(STATUS_NAMES).map(([id, name]) => ({
  id: String(id),
  name,
}));

class RouteFake {
  constructor(flow, options = {}) {
    this.flow = flow;
    this.current = "10206";
    this.writes = 0;
    this.transitionReads = 0;
    this.failReadsAfterWrites = Infinity;
    this.calls = [];
    this.statusCatalog =
      options.statusCatalog !== undefined
        ? options.statusCatalog
        : structuredClone(STATUS_CATALOG);
  }

  transitionById(id) {
    for (const list of Object.values(this.flow)) {
      const found = (list ?? []).find((item) => String(item.id) === String(id));
      if (found) return found;
    }
    return null;
  }

  async request(request) {
    this.calls.push(request);
    if (request.path.endsWith("/issue/details.json"))
      return { payload: { key: "LEL-42", id: 42, isSubtask: false } };
    if (request.path.endsWith("/issue/transitions.json")) {
      this.transitionReads += 1;
      return {
        payload: {
          issueId: 42,
          transitions: structuredClone(this.flow[this.current] ?? []),
        },
      };
    }
    if (request.path.endsWith("/work/allData.json"))
      return { payload: structuredClone(BOARD) };
    if (request.path === "/rest/api/2/status")
      return { payload: structuredClone(this.statusCatalog) };
    if (request.path === "/rest/api/2/issue/LEL-42") {
      if (this.writes > this.failReadsAfterWrites)
        throw new JiraProtocolError("fixture readback offline");
      return {
        payload: {
          key: "LEL-42",
          id: "42",
          fields: {
            status: {
              id: this.current,
              name: STATUS_NAMES[this.current] ?? "?",
            },
            assignee: null,
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10100", name: "Task" },
          },
        },
      };
    }
    if (request.path.endsWith("/transitionAndRank")) {
      this.writes += 1;
      const picked = this.transitionById(request.json.selectedTransitionId);
      if (!picked) throw new Error("unknown transition in fixture");
      this.current = String(picked.targetStatus);
      return { payload: {} };
    }
    throw new Error(`unexpected request ${request.path}`);
  }

  mutations() {
    return this.calls.filter((call) =>
      String(call.path ?? "").endsWith("/transitionAndRank")
    );
  }
}

function hop(id, name, targetStatus, form = false) {
  return { id, name, targetStatus, hasTransitionView: form };
}

test("direct route plans one step and completes with readback", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
    10217: [],
  });
  const preview = await planTransitionRoute(transport, {
    issueKey: "lel-42",
    status: "10217",
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.targetStatus, "10217");
  assert.equal(preview.firstStep.transitionId, 41);
  assert.equal(preview.firstStep.targetColumn, 5934577);
  assert.equal(preview.routeResolution, "direct");
  assert.equal(transport.mutations().length, 0);
  const done = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "10217",
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.writes, 1);
  assert.equal(done.steps.length, 1);
  assert.equal(done.steps[0].toStatus, "10217");
  assert.equal(assertHandoffClean(done.handoff), true);
  assert.doesNotMatch(serializeReceipt(done), /cookie/i);
});

test("multi-step route resolves each step live and verifies every hop", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
    10217: [hop(42, "To Review", "10218")],
    10218: [],
  });
  const preview = await planTransitionRoute(transport, {
    issueKey: "LEL-42",
    status: "10218",
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.firstStep.transitionId, 41);
  assert.equal(preview.routeResolution, "live-stepwise");
  const readsBefore = transport.transitionReads;
  assert.ok(readsBefore >= 1);
  const done = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "10218",
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.writes, 2);
  assert.deepEqual(
    done.steps.map((entry) => entry.toStatus),
    ["10217", "10218"]
  );
  assert.ok(transport.transitionReads > readsBefore);
  assert.equal(assertHandoffClean(done.handoff), true);
});

test("already-reached status completes without any write", async () => {
  const transport = new RouteFake({ 10206: [hop(41, "In", "10217")] });
  const done = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "10206",
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.alreadyInStatus, true);
  assert.equal(done.writes, 0);
  assert.equal(transport.mutations().length, 0);
});

test("stale live state invalidates the confirmation without writing", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
    10217: [],
  });
  const preview = await planTransitionRoute(transport, {
    issueKey: "LEL-42",
    status: "10217",
  });
  transport.flow["10206"][0].name = "Renamed transition";
  const second = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "10217",
    confirm: preview.confirmation,
  });
  assert.equal(second.state, "AwaitingConfirmation");
  assert.notEqual(second.confirmation, preview.confirmation);
  assert.equal(transport.mutations().length, 0);
});

test("changes after confirmation revalidation stop before a write", async () => {
  for (const change of ["state", "step"]) {
    const transport = new RouteFake({
      10206: [hop(41, "In Progress", "10217")],
      10217: [hop(42, "To Review", "10218")],
    });
    const preview = await planTransitionRoute(transport, {
      issueKey: "LEL-42",
      status: "10218",
    });
    const request = transport.request.bind(transport);
    let stateReads = 0;
    let transitionReads = 0;
    transport.request = async (call) => {
      if (
        call.path === "/rest/api/2/issue/LEL-42" &&
        ++stateReads === 2 &&
        change === "state"
      )
        transport.current = "10217";
      if (
        call.path.endsWith("/issue/transitions.json") &&
        ++transitionReads === 2 &&
        change === "step"
      )
        transport.flow["10206"] = [hop(43, "Changed", "10217")];
      return request(call);
    };
    await assert.rejects(
      transitionToStatus(transport, {
        issueKey: "LEL-42",
        status: "10218",
        confirm: preview.confirmation,
      }),
      /changed after confirmation/
    );
    assert.equal(transport.mutations().length, 0);
  }
});

test("unknown outcome stops the route after verified steps without retry", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
    10217: [hop(42, "To Review", "10218")],
    10218: [],
  });
  transport.failReadsAfterWrites = 1;
  const preview = await planTransitionRoute(transport, {
    issueKey: "LEL-42",
    status: "10218",
  });
  await assert.rejects(
    transitionToStatus(transport, {
      issueKey: "LEL-42",
      status: "10218",
      confirm: preview.confirmation,
    }),
    (error) => {
      assert.match(error.message, /stopped after 1 verified step/);
      return true;
    }
  );
  assert.equal(transport.mutations().length, 2);
});

test("unreachable, ambiguous, and form-only routes fail with no write", async () => {
  const unreachable = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
  });
  await assert.rejects(
    planTransitionRoute(unreachable, { issueKey: "LEL-42", status: "99999" }),
    /not on board/
  );
  assert.equal(unreachable.mutations().length, 0);

  const none = new RouteFake({ 10206: [] });
  await assert.rejects(
    planTransitionRoute(none, { issueKey: "LEL-42", status: "10217" }),
    /no transition is currently available/
  );

  const formed = new RouteFake({
    10206: [hop(41, "Needs Form", "10217", true)],
  });
  await assert.rejects(
    planTransitionRoute(formed, { issueKey: "LEL-42", status: "10217" }),
    /workflow form/
  );

  const ambiguous = new RouteFake({
    10206: [hop(41, "A", "10217"), hop(43, "B", "10217")],
  });
  await assert.rejects(
    planTransitionRoute(ambiguous, { issueKey: "LEL-42", status: "10218" }),
    /no direct transition to status 10218/
  );
  await assert.rejects(
    planTransitionRoute(
      new RouteFake({
        10206: [hop(41, "A", "10217"), hop(44, "B", "10217")],
      }),
      { issueKey: "LEL-42", status: "10217" }
    ),
    /several transitions reach status 10217/
  );

  const looping = new RouteFake({
    10206: [hop(41, "Forth", "10217")],
    10217: [hop(42, "Back", "10206")],
    10218: [],
  });
  const preview = await planTransitionRoute(looping, {
    issueKey: "LEL-42",
    status: "10218",
  });
  await assert.rejects(
    transitionToStatus(looping, {
      issueKey: "LEL-42",
      status: "10218",
      confirm: preview.confirmation,
    }),
    /possible loop/
  );
});

test("status must be a live board status id or unambiguous name", async () => {
  const transport = new RouteFake({ 10206: [] });
  for (const bad of ["0", "", "10.5", "   "])
    await assert.rejects(
      planTransitionRoute(transport, { issueKey: "LEL-42", status: bad }),
      /status must be a Jira status id or unambiguous status name/
    );
  await assert.rejects(
    planTransitionRoute(transport, { issueKey: "LEL-42", status: "Done" }),
    /no board status named/
  );
  assert.equal(transport.mutations().length, 0);
  assert.ok(ROUTE_MAX_STEPS >= 2);
});

test("step limit guards a route that never arrives", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "Forth", "10217")],
    10217: [hop(42, "Onward", "10218")],
    10218: [hop(43, "Away", "10217")],
  });
  const preview = await planTransitionRoute(transport, {
    issueKey: "LEL-42",
    status: "10218",
  });
  await assert.rejects(
    transitionToStatus(transport, {
      issueKey: "LEL-42",
      status: "10218",
      confirm: preview.confirmation,
      maxSteps: 1,
    }),
    /within 1 step/
  );
  assert.equal(transport.mutations().length, 1);
});

test("named target resolves case-insensitively and completes direct", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
    10217: [],
  });
  const preview = await planTransitionRoute(transport, {
    issueKey: "LEL-42",
    status: "  in PROGRESS ",
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.targetStatus, "10217");
  assert.equal(preview.target.statusId, "10217");
  assert.equal(transport.mutations().length, 0);
  const done = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "  in PROGRESS ",
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.writes, 1);
  assert.equal(done.steps[0].toStatus, "10217");
  assert.equal(assertHandoffClean(done.handoff), true);
});

test("named multi-step route discovers the later step live", async () => {
  const transport = new RouteFake({
    10206: [hop(41, "In Progress", "10217")],
    10217: [hop(42, "To Review", "10218")],
    10218: [],
  });
  const preview = await planTransitionRoute(transport, {
    issueKey: "LEL-42",
    status: "Review",
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.targetStatus, "10218");
  assert.equal(preview.routeResolution, "live-stepwise");
  const done = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "Review",
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "Completed");
  assert.deepEqual(
    done.steps.map((entry) => entry.toStatus),
    ["10217", "10218"]
  );
});

test("named already-reached status completes without a write", async () => {
  const transport = new RouteFake({ 10206: [hop(41, "In", "10217")] });
  const done = await transitionToStatus(transport, {
    issueKey: "LEL-42",
    status: "TO do",
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.alreadyInStatus, true);
  assert.equal(done.writes, 0);
  assert.equal(transport.mutations().length, 0);
});

test("ambiguous named target stops with no write", async () => {
  const transport = new RouteFake(
    { 10206: [hop(41, "In Progress", "10217")] },
    {
      statusCatalog: [
        { id: "10217", name: "Review" },
        { id: "10218", name: "review " },
      ],
    }
  );
  await assert.rejects(
    planTransitionRoute(transport, { issueKey: "LEL-42", status: "Review" }),
    /ambiguous status name/
  );
  assert.equal(transport.mutations().length, 0);
});
