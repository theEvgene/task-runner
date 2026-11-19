import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import {
  CONTRACT,
  assertIssueKey,
  getIssueState,
  getIssueTransitions,
  transitionIssue,
} from "./operations.mjs";
import { toHandoffEvidence } from "./handoff.mjs";

export const ROUTE_MAX_STEPS = 10;

function observedStatus(result) {
  const raw = Number(result?.response?.status);
  return Number.isSafeInteger(raw) && raw >= 100 && raw <= 599 ? raw : "unknown";
}

function observingTransport(transport, statuses) {
  return {
    origin: transport.origin,
    atlToken: transport.atlToken,
    request: async (request) => {
      const result = await transport.request(request);
      const key = `${String(request.method ?? "GET").toUpperCase()} ${String(request.path ?? "")}`;
      statuses.set(key, observedStatus(result));
      return result;
    },
  };
}

function statusFor(statuses, method, path) {
  return statuses.get(`${method} ${path}`) ?? "unknown";
}

async function boardColumns(transport) {
  const { payload } = await transport.request({
    path: "/rest/greenhopper/1.0/xboard/work/allData.json",
    query: {
      rapidViewId: CONTRACT.rapidViewId,
      selectedProjectKey: CONTRACT.projectKey,
    },
  });
  if (
    payload?.rapidViewId !== CONTRACT.rapidViewId ||
    !Array.isArray(payload?.columnsData?.columns)
  )
    throw new JiraProtocolError("board data returned an unsupported body");
  return payload.columnsData.columns;
}

function columnFor(columns, targetStatus) {
  const matches = columns.filter((column) =>
    (Array.isArray(column?.statusIds) ? column.statusIds : [])
      .map(String)
      .includes(String(targetStatus))
  );
  return matches;
}

function usableTransitions(transitions) {
  return (Array.isArray(transitions) ? transitions : []).filter(
    (item) => item && item.hasTransitionView === false
  );
}

function compactTransitions(transitions) {
  return (Array.isArray(transitions) ? transitions : []).map((item) => ({
    id: String(item?.id ?? ""),
    name: String(item?.name ?? ""),
    targetStatus: String(item?.targetStatus ?? ""),
    blockedByForm: item?.hasTransitionView !== false,
  }));
}

// Read-only route plan from the current live state to a target status id or
// unambiguous live status name.
// No static workflow map is consulted: every edge comes from the
// issue-specific transitions actually returned for the current state, and
// every column check comes from the live board. A non-numeric target is
// resolved against the live status catalog filtered to the live board; the
// match must be unique after trimming, collapsing whitespace, and
// case-folding. Returns a Completed receipt
// when already in the target status, a plannable first step otherwise, and
// throws fail-closed when the route is unavailable or ambiguous.
function normalizeStatusName(value) {
  return String(value ?? "")
    .replace(/[\t\n\f\r ]+/g, " ")
    .trim()
    .toLowerCase();
}

async function resolveTargetStatus(watched, columns, rawStatus) {
  const input = String(rawStatus ?? "").trim();
  if (!input)
    throw new JiraProtocolError(
      "status must be a Jira status id or unambiguous status name"
    );
  if (/^[1-9]\d*$/.test(input)) {
    if (!columns.some((column) => columnFor([column], input).length > 0))
      throw new JiraProtocolError(
        `status ${input} is not on board ${CONTRACT.rapidViewId}`
      );
    return { id: input, name: null, query: null };
  }
  if (/^[0-9.\s]+$/.test(input))
    throw new JiraProtocolError(
      "status must be a Jira status id or unambiguous status name"
    );
  const wanted = normalizeStatusName(input);
  if (!wanted)
    throw new JiraProtocolError(
      "status must be a Jira status id or unambiguous status name"
    );
  const { payload } = await watched.request({
    path: "/rest/api/2/status",
  });
  if (!Array.isArray(payload))
    throw new JiraProtocolError("status catalog returned an unsupported body");
  const boardIds = new Set(
    columns.flatMap((column) =>
      (Array.isArray(column?.statusIds) ? column.statusIds : []).map(String)
    )
  );
  const onBoard = payload.filter(
    (entry) =>
      entry &&
      typeof entry === "object" &&
      boardIds.has(String(entry.id)) &&
      typeof entry.name === "string"
  );
  const matched = onBoard.filter(
    (entry) => normalizeStatusName(entry.name) === wanted
  );
  if (matched.length === 0)
    throw new JiraProtocolError(
      `no board status named "${input}" is on board ${CONTRACT.rapidViewId}`
    );
  if (matched.length > 1) {
    const options = matched.map(
      (entry) => `${String(entry.id)} (${String(entry.name)})`
    );
    throw new JiraProtocolError(
      `ambiguous status name "${input}": ${options.join(", ")}`
    );
  }
  return {
    id: String(matched[0].id),
    name: String(matched[0].name),
    query: input,
  };
}

export async function planTransitionRoute(transport, { issueKey, status }) {
  const key = assertIssueKey(issueKey);
  const rawTarget = String(status ?? "").trim();
  if (!rawTarget)
    throw new JiraProtocolError(
      "status must be a Jira status id or unambiguous status name"
    );
  const observed = new Map();
  const watched = observingTransport(transport, observed);
  const [state, discovered, columns] = await Promise.all([
    getIssueState(watched, key),
    getIssueTransitions(watched, key),
    boardColumns(watched),
  ]);
  const currentStatus = String(state.fields.status?.id ?? "");
  const currentName = String(state.fields.status?.name ?? "");
  if (!/^[1-9]\d*$/.test(currentStatus))
    throw new JiraProtocolError("issue state did not report a status id");
  const resolved = await resolveTargetStatus(watched, columns, rawTarget);
  const target = resolved.id;
  const operation = {
    kind: "transition-to-status",
    issueKey: key,
    targetStatus: target,
    ...(resolved.query !== null
      ? { targetQuery: resolved.query, targetStatusName: resolved.name }
      : {}),
  };
  const issue = {
    key,
    id: Number(discovered.issue.id),
    currentStatus,
    currentStatusName: currentName,
  };
  if (currentStatus === target) {
    return {
      state: "Completed",
      operation,
      issue,
      target: {
        statusId: target,
        ...(resolved.name !== null ? { statusName: resolved.name } : {}),
      },
      steps: [],
      writes: 0,
      alreadyInStatus: true,
      handoff: [],
    };
  }
  const usable = usableTransitions(discovered.transitions);
  const direct = usable.filter(
    (item) => String(item.targetStatus) === target
  );
  let firstStep = null;
  let resolution = "live-stepwise";
  if (direct.length > 0) {
    if (direct.length > 1) {
      // Several distinct transitions reach the target directly; choosing
      // among them needs an explicit single-step transition call instead.
      const options = direct.map(
        (item) => `${item.id} (${item.name ?? "unnamed"})`
      );
      throw new JiraProtocolError(
        `several transitions reach status ${target}: ${options.join(", ")}; use the single-step transition command with an explicit transition id`
      );
    }
    firstStep = describeStep(columns, direct[0]);
    resolution = "direct";
  } else if (usable.length === 0) {
    const formed = (Array.isArray(discovered.transitions)
      ? discovered.transitions
      : []
    ).filter((item) => item && item.hasTransitionView !== false);
    if (formed.length > 0)
      throw new JiraProtocolError(
        "the only available transitions require a workflow form and are unsupported by this CLI"
      );
    throw new JiraProtocolError(
      `no transition is currently available from status ${currentStatus}`
    );
  } else if (usable.length === 1) {
    firstStep = describeStep(columns, usable[0]);
  } else {
    const options = usable.map(
      (item) =>
        `${item.id} (${item.name ?? "unnamed"}) -> ${String(item.targetStatus)}`
    );
    throw new JiraProtocolError(
      `no direct transition to status ${target}; ambiguous next step: ${options.join(", ")}`
    );
  }
  const plan = {
    operation,
    issue,
    target: {
      statusId: target,
      ...(resolved.name !== null ? { statusName: resolved.name } : {}),
      ...(resolved.query !== null ? { statusQuery: resolved.query } : {}),
    },
    current: { statusId: currentStatus, statusName: currentName },
    firstStep,
    routeResolution: resolution,
    available: compactTransitions(discovered.transitions),
    handoff: [
      toHandoffEvidence({
        stage: "route_state",
        method: "GET",
        path: `/rest/api/2/issue/${key}`,
        httpStatus: statusFor(observed, "GET", `/rest/api/2/issue/${key}`),
      }),
      toHandoffEvidence({
        stage: "route_detail",
        method: "GET",
        path: "/rest/greenhopper/1.0/xboard/issue/details.json",
        httpStatus: statusFor(
          observed,
          "GET",
          "/rest/greenhopper/1.0/xboard/issue/details.json"
        ),
      }),
      toHandoffEvidence({
        stage: "route_transitions",
        method: "GET",
        path: "/rest/greenhopper/1.0/xboard/issue/transitions.json",
        httpStatus: statusFor(
          observed,
          "GET",
          "/rest/greenhopper/1.0/xboard/issue/transitions.json"
        ),
      }),
      toHandoffEvidence({
        stage: "route_board",
        method: "GET",
        path: "/rest/greenhopper/1.0/xboard/work/allData.json",
        httpStatus: statusFor(
          observed,
          "GET",
          "/rest/greenhopper/1.0/xboard/work/allData.json"
        ),
        note: "read-only discovery; no write performed",
      }),
      ...(resolved.query !== null
        ? [
            toHandoffEvidence({
              stage: "route_statuses",
              method: "GET",
              path: "/rest/api/2/status",
              httpStatus: statusFor(observed, "GET", "/rest/api/2/status"),
              note: "read-only name resolution; no write performed",
            }),
          ]
        : []),
    ],
  };
  return {
    state: "AwaitingConfirmation",
    ...plan,
    confirmation: confirmationDigest({ operation, firstStep, currentStatus }),
  };
}

function describeStep(columns, transition) {
  const targetStatus = String(transition.targetStatus);
  const candidates = columnFor(columns, targetStatus);
  if (candidates.length === 0)
    throw new JiraProtocolError(
      `transition target status ${targetStatus} is not on board ${CONTRACT.rapidViewId}`
    );
  if (candidates.length > 1)
    throw new JiraProtocolError(
      `transition target status ${targetStatus} sits in several columns; pick an explicit single-step transition instead`
    );
  const column = candidates[0];
  return {
    transitionId: Number(transition.id),
    transitionName: String(transition.name ?? ""),
    targetStatus,
    targetColumn: Number(column.id),
    targetColumnName: String(column.name ?? ""),
  };
}

// Confirmed multi-step executor. The confirmation digest binds the planned
// first step; every later step is re-discovered live and verified by the
// single-step transition readback before the next one starts. An unknown
// step outcome stops the route without an automatic retry.
export async function transitionToStatus(
  transport,
  { issueKey, status, confirm, maxSteps = ROUTE_MAX_STEPS }
) {
  const planned = await planTransitionRoute(transport, { issueKey, status });
  if (planned.state === "Completed") return planned;
  if (confirm !== planned.confirmation)
    return { state: "AwaitingConfirmation", ...planned };
  const limit = Number(maxSteps);
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > ROUTE_MAX_STEPS)
    throw new JiraProtocolError(
      `max-steps must be an integer between 1 and ${ROUTE_MAX_STEPS}`
    );
  const steps = [];
  const visited = new Set([planned.current.statusId]);
  const stepObserved = new Map();
  const watched = observingTransport(transport, stepObserved);
  for (let index = 0; index < limit; index += 1) {
    const live = await getIssueState(watched, planned.operation.issueKey);
    const current = String(live.fields.status?.id ?? "");
    if (current === planned.target.statusId) {
      return {
        state: "Completed",
        operation: planned.operation,
        issue: planned.issue,
        target: planned.target,
        steps,
        writes: steps.length,
        handoff: steps.flatMap((entry) => entry.handoff),
      };
    }
    if (visited.has(current) && steps.length > 0)
      throw new JiraProtocolError(
        `route revisited status ${current}; refusing a possible loop`
      );
    visited.add(current);
    const discovered = await getIssueTransitions(
      watched,
      planned.operation.issueKey
    );
    const usable = usableTransitions(discovered.transitions);
    const direct = usable.filter(
      (item) => String(item.targetStatus) === planned.target.statusId
    );
    let next;
    if (direct.length === 1) next = direct[0];
    else if (direct.length > 1)
      throw new JiraProtocolError(
        "route reached an ambiguous direct choice; use the single-step transition command instead"
      );
    else if (usable.length === 1) next = usable[0];
    else if (usable.length === 0)
      throw new JiraProtocolError(
        `route stalled: no transition is available from status ${current}`
      );
    else
      throw new JiraProtocolError(
        `route stalled: ambiguous next step from status ${current}`
      );
    const columns = await boardColumns(watched);
    const step = describeStep(columns, next);
    const from = current;
    let done;
    try {
      const preview = await transitionIssue(watched, {
        issueKey: planned.operation.issueKey,
        transitionId: step.transitionId,
        targetColumn: step.targetColumn,
      });
      if (preview.state !== "AwaitingConfirmation")
        throw new JiraProtocolError("single-step transition refused to plan");
      done = await transitionIssue(watched, {
        issueKey: planned.operation.issueKey,
        transitionId: step.transitionId,
        targetColumn: step.targetColumn,
        confirm: preview.confirmation,
      });
    } catch (error) {
      if (error instanceof JiraAmbiguousOutcome)
        throw new JiraAmbiguousOutcome(
          `transition-to-status stopped after ${steps.length} verified step(s): step outcome is unknown; re-plan from the current status before any new run`,
          { cause: error }
        );
      throw error;
    }
    if (done.state !== "Completed")
      throw new JiraProtocolError("single-step transition refused to confirm");
    steps.push({
      transitionId: step.transitionId,
      transitionName: step.transitionName,
      fromStatus: from,
      toStatus: String(done.readback.statusId),
      handoff: [
        toHandoffEvidence({
          stage: "route_step",
          method: "PUT",
          path: "/rest/greenhopper/1.0/xboard/transitionAndRank",
          httpStatus: statusFor(
            stepObserved,
            "PUT",
            "/rest/greenhopper/1.0/xboard/transitionAndRank"
          ),
        }),
        toHandoffEvidence({
          stage: "route_step_readback",
          method: "GET",
          path: `/rest/api/2/issue/${planned.operation.issueKey}`,
          httpStatus: statusFor(
            stepObserved,
            "GET",
            `/rest/api/2/issue/${planned.operation.issueKey}`
          ),
        }),
      ],
    });
  }
  const live = await getIssueState(watched, planned.operation.issueKey);
  if (String(live.fields.status?.id ?? "") === planned.target.statusId) {
    return {
      state: "Completed",
      operation: planned.operation,
      issue: planned.issue,
      target: planned.target,
      steps,
      writes: steps.length,
      handoff: steps.flatMap((entry) => entry.handoff),
    };
  }
  throw new JiraProtocolError(
    `route did not reach status ${planned.target.statusId} within ${limit} step(s)`
  );
}
