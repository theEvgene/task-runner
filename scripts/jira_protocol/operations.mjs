import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import { createFormFromModel, defaultsFromEditHtml } from "./html-form.mjs";

export const CONTRACT = Object.freeze({
  origin: "https://jira3.tcsbank.ru",
  projectKey: "LEL",
  projectId: 10848,
  epicIssueTypeId: 10000,
  rapidViewId: 298290,
});

export function assertIssueKey(issueKey) {
  const normalized = String(issueKey ?? "")
    .trim()
    .toUpperCase();
  if (!/^LEL-\d+$/.test(normalized))
    throw new JiraProtocolError("issue must be an LEL issue key");
  return normalized;
}

function integer(value, name) {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0)
    throw new JiraProtocolError(`${name} must be a positive integer`);
  return parsed;
}

export async function getIssue(transport, issueKey) {
  const key = assertIssueKey(issueKey);
  const { payload } = await transport.request({
    path: "/rest/greenhopper/1.0/xboard/issue/details.json",
    query: {
      rapidViewId: CONTRACT.rapidViewId,
      issueIdOrKey: key,
      loadSubtasks: true,
    },
  });
  if (!payload || typeof payload !== "object" || payload.key !== key)
    throw new JiraProtocolError(
      "issue details did not match the requested key"
    );
  if (!Number.isSafeInteger(Number(payload.id)))
    throw new JiraProtocolError("issue details did not contain a numeric id");
  return payload;
}

export async function getIssueState(
  transport,
  issueKey,
  requestedFields = ["status", "assignee", "project", "issuetype"]
) {
  const key = assertIssueKey(issueKey);
  const fields = [...new Set(requestedFields.map(String))];
  if (
    fields.length === 0 ||
    fields.some(
      (field) => !/^(?:[a-z][a-z0-9_]*|customfield_\d+)$/i.test(field)
    )
  )
    throw new JiraProtocolError("issue readback fields are invalid");
  const { payload } = await transport.request({
    path: `/rest/api/2/issue/${key}`,
    query: { fields: fields.join(",") },
  });
  if (
    !payload ||
    typeof payload !== "object" ||
    payload.key !== key ||
    !payload.fields ||
    typeof payload.fields !== "object"
  )
    throw new JiraProtocolError("issue state did not match the requested key");
  return payload;
}

export async function getIssueTransitions(transport, issueKey) {
  const issue = await getIssue(transport, issueKey);
  const { payload } = await transport.request({
    path: "/rest/greenhopper/1.0/xboard/issue/transitions.json",
    query: { issueId: issue.id },
  });
  if (!payload || !Array.isArray(payload.transitions))
    throw new JiraProtocolError(
      "issue transitions returned an unsupported body"
    );
  return { issue, transitions: payload.transitions };
}

async function boardData(transport) {
  const { payload } = await transport.request({
    path: "/rest/greenhopper/1.0/xboard/work/allData.json",
    query: {
      rapidViewId: CONTRACT.rapidViewId,
      selectedProjectKey: CONTRACT.projectKey,
    },
  });
  if (
    payload?.rapidViewId !== CONTRACT.rapidViewId ||
    !Array.isArray(payload?.columnsData?.columns) ||
    !Array.isArray(payload?.issuesData?.issues)
  )
    throw new JiraProtocolError("board data returned an unsupported body");
  return payload;
}

async function backlogMatch(transport, key) {
  const { payload } = await transport.request({
    path: "/rest/greenhopper/1.0/xboard/issue/matchesBoard",
    query: {
      rapidViewId: CONTRACT.rapidViewId,
      issueIdsOrKeys: key,
    },
  });
  if (
    !payload ||
    !Array.isArray(payload.issueIdsOrKeys) ||
    !Array.isArray(payload.issuesInBacklog)
  )
    throw new JiraProtocolError(
      "board membership readback returned an unsupported body"
    );
  return payload;
}

function validStatusId(value) {
  return typeof value === "string"
    ? /^[1-9]\d*$/.test(value)
    : Number.isSafeInteger(value) && value > 0;
}

export async function planTransition(
  transport,
  { issueKey, transitionId, targetColumn }
) {
  const key = assertIssueKey(issueKey);
  const selectedTransitionId = integer(transitionId, "transition-id");
  const selectedTargetColumn = integer(targetColumn, "target-column");
  const [{ issue, transitions }, board] = await Promise.all([
    getIssueTransitions(transport, key),
    boardData(transport),
  ]);
  const transition = transitions.find(
    (item) => Number(item?.id) === selectedTransitionId
  );
  if (!transition)
    throw new JiraProtocolError("transition is not currently available");
  if (transition.hasTransitionView !== false)
    throw new JiraProtocolError(
      "transition requires a workflow form and is unsupported by this CLI"
    );
  const column = board.columnsData.columns.find(
    (item) => Number(item?.id) === selectedTargetColumn
  );
  if (!column)
    throw new JiraProtocolError(
      `target column is not on board ${CONTRACT.rapidViewId}`
    );
  if (!column.statusIds.map(String).includes(String(transition.targetStatus)))
    throw new JiraProtocolError(
      "target column does not contain the transition target status"
    );
  const operation = {
    kind: "transition",
    issueKey: key,
    transitionId: selectedTransitionId,
    transitionName: String(transition.name ?? ""),
    targetColumn: selectedTargetColumn,
    targetColumnName: String(column.name ?? ""),
    targetStatus: String(transition.targetStatus),
  };
  return {
    operation,
    confirmation: confirmationDigest(operation),
    issue: {
      key,
      id: Number(issue.id),
      isSubtask: Boolean(issue.isSubtask),
    },
    transition: {
      id: selectedTransitionId,
      name: transition.name,
      targetStatus: String(transition.targetStatus),
    },
    column: { id: selectedTargetColumn, name: column.name },
  };
}

export async function transitionIssue(transport, input) {
  const plan = await planTransition(transport, input);
  if (input.confirm !== plan.confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  await transport.request({
    method: "PUT",
    path: "/rest/greenhopper/1.0/xboard/transitionAndRank",
    json: {
      rapidViewId: CONTRACT.rapidViewId,
      issueKeys: [plan.issue.key],
      targetColumn: plan.column.id,
      selectedTransitionId: plan.transition.id,
    },
    mutation: true,
  });
  let readback;
  try {
    readback = await getIssueState(transport, plan.issue.key);
  } catch (error) {
    throw new JiraAmbiguousOutcome("transition readback failed", {
      cause: error,
    });
  }
  if (String(readback.fields.status?.id) !== plan.transition.targetStatus)
    throw new JiraAmbiguousOutcome(
      "transition response could not be confirmed by issue readback"
    );
  return {
    state: "Completed",
    ...plan,
    readback: { statusId: String(readback.fields.status.id) },
  };
}

export async function assignIssue(transport, { issueKey, assignee, confirm }) {
  const key = assertIssueKey(issueKey);
  const login = String(assignee ?? "").trim();
  if (!/^[a-z0-9._@-]+$/i.test(login))
    throw new JiraProtocolError("assignee must be a Jira login");
  const issue = await getIssue(transport, key);
  const operation = { kind: "assign", issueKey: key, assignee: login };
  const confirmation = confirmationDigest(operation);
  const plan = {
    operation,
    confirmation,
    issue: { key, id: Number(issue.id), isSubtask: Boolean(issue.isSubtask) },
  };
  if (confirm !== confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  if (!transport.atlToken)
    throw new JiraProtocolError("JIRA_ATL_TOKEN is required for assignment");
  await transport.request({
    path: "/secure/AssignIssue.jspa",
    query: {
      atl_token: transport.atlToken,
      id: issue.id,
      assignee: login,
    },
    expect: "text",
    mutation: true,
  });
  let readback;
  try {
    readback = await getIssueState(transport, key);
  } catch (error) {
    throw new JiraAmbiguousOutcome("assignment readback failed", {
      cause: error,
    });
  }
  const readbackLogin =
    readback.fields.assignee?.name ?? readback.fields.assignee?.key;
  if (readbackLogin !== login)
    throw new JiraAmbiguousOutcome(
      "assignment response could not be confirmed by issue readback"
    );
  return {
    state: "Completed",
    ...plan,
    readback: { assignee: readbackLogin },
  };
}

async function createRequest(transport, stage, request) {
  try {
    return await transport.request(request);
  } catch (error) {
    if (
      error instanceof JiraProtocolError ||
      error instanceof JiraAmbiguousOutcome
    )
      error.stage = stage;
    throw error;
  }
}

export async function createEpic(
  transport,
  { summary, description, fields = {}, confirm }
) {
  const title = String(summary ?? "").trim();
  if (!title) throw new JiraProtocolError("summary is required");
  const suppliedFields = {
    ...fields,
    summary: title,
    ...(description !== undefined ? { description: String(description) } : {}),
  };
  const operation = {
    kind: "create-lel-backlog-epic",
    projectId: CONTRACT.projectId,
    issueTypeId: CONTRACT.epicIssueTypeId,
    fields: suppliedFields,
  };
  const { payload: model } = await createRequest(transport, "create_form_model", {
    method: "POST",
    path: "/secure/QuickCreateIssue!default.jspa",
    query: {
      decorator: "none",
      issuetype: CONTRACT.epicIssueTypeId,
      pid: CONTRACT.projectId,
    },
  });
  const { form, missing, safeForm, modelFields } = createFormFromModel(
    model,
    suppliedFields
  );
  const allowedFields = new Set(modelFields.map((field) => field.id));
  const unknownFields = Object.keys(suppliedFields).filter(
    (field) => !allowedFields.has(field)
  );
  if (unknownFields.length)
    throw new JiraProtocolError(
      `fields are absent from the live Quick Create model: ${unknownFields.join(", ")}`
    );
  const safeModel = {
    requiredFields: model.fields
      .filter((field) => field?.required)
      .map((field) => ({ id: field.id, label: field.label ?? field.id })),
    missingFields: missing,
  };
  const confirmationScope = {
    operation,
    submission: {
      path: "/secure/QuickCreateIssue.jspa",
      query: { decorator: "none" },
      form: safeForm,
      modelFields,
    },
  };
  const confirmation = confirmationDigest(confirmationScope);
  const plan = {
    operation,
    confirmation,
    model: safeModel,
  };
  if (missing.length) return { state: "MissingRequiredFields", ...plan };
  if (confirm !== confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  const { payload } = await createRequest(transport, "create_submission", {
    method: "POST",
    path: "/secure/QuickCreateIssue.jspa",
    query: { decorator: "none" },
    form,
    mutation: true,
    expect: "text",
  });
  if (
    !payload ||
    typeof payload !== "object" ||
    !/^LEL-\d+$/.test(String(payload.issueKey ?? ""))
  )
    throw new JiraAmbiguousOutcome(
      "Quick Create did not return a confirmed LEL issue key"
    );
  const verified = await verifyCreatedEpic(transport, payload.issueKey, suppliedFields);
  return { ...plan, ...verified };
}

export async function verifyCreatedEpic(transport, issueKey, suppliedFields) {
  if (typeof issueKey !== "string" || !/^LEL-\d+$/.test(issueKey))
    throw new JiraProtocolError("a valid LEL issue key is required for readback");
  let issue;
  let membership;
  let board;
  try {
    const readbackFields = [
      "project",
      "issuetype",
      ...Object.keys(suppliedFields),
    ];
    [issue, membership] = await Promise.all([
      getIssueState(transport, issueKey, readbackFields),
      backlogMatch(transport, issueKey),
    ]);
    if (!membership.issuesInBacklog.includes(issueKey))
      board = await boardData(transport);
  } catch (error) {
    throw new JiraAmbiguousOutcome("created Epic readback failed", {
      cause: error,
      issueKey,
    });
  }
  const isExpectedProject =
    issue.fields.project?.key === CONTRACT.projectKey &&
    Number(issue.fields.project?.id) === CONTRACT.projectId;
  const isEpic =
    String(issue.fields.issuetype?.id) === String(CONTRACT.epicIssueTypeId) &&
    issue.fields.issuetype?.name === "Epic";
  const isInBacklog = membership.issuesInBacklog.includes(issueKey);
  const isOnBoard = board
    ? board.issuesData.issues.some(
        (candidate) =>
          candidate.key === issueKey &&
          validStatusId(candidate.statusId) &&
          board.columnsData.columns.some(
            (column) =>
              Array.isArray(column.statusIds) &&
              column.statusIds.some(
                (statusId) =>
                  validStatusId(statusId) &&
                  String(statusId) === String(candidate.statusId)
              )
          )
      )
    : null;
  const persistedFields = Object.fromEntries(
    Object.entries(suppliedFields).map(([field, expected]) => {
      const actual = issue.fields[field];
      return [
        field,
        {
          expected,
          actual,
          matched: persistedValueMatches(actual, expected),
        },
      ];
    })
  );
  const fieldsPersisted = Object.values(persistedFields).every(
    (field) => field.matched
  );
  if (
    !isExpectedProject ||
    !isEpic ||
    (!isInBacklog && !isOnBoard) ||
    !fieldsPersisted
  )
    throw new JiraAmbiguousOutcome(
      "created issue could not be confirmed with the requested persisted fields as an LEL Epic on the board or in its backlog",
      {
        issueKey,
        verification: { isExpectedProject, isEpic, isInBacklog, isOnBoard, fieldsPersisted },
      }
    );
  return {
    state: "Completed",
    issue: { key: issue.key, id: Number(issue.id) },
    readback: {
      projectKey: issue.fields.project.key,
      projectId: Number(issue.fields.project.id),
      issueTypeId: String(issue.fields.issuetype.id),
      issueTypeName: issue.fields.issuetype.name,
      inBacklog: isInBacklog,
      onBoard: isOnBoard,
      persistedFields,
    },
  };
}

function persistedValueMatches(actual, expected) {
  const wanted = String(expected);
  if (actual === null || actual === undefined) return false;
  if (["string", "number", "boolean"].includes(typeof actual))
    return String(actual) === wanted;
  if (Array.isArray(actual))
    return actual.length === 1 && persistedValueMatches(actual[0], wanted);
  if (typeof actual === "object") {
    const candidates = [
      actual.value,
      actual.id,
      actual.key,
      actual.name,
    ].filter((value) => value !== null && value !== undefined);
    return (
      candidates.length > 0 &&
      candidates.some((value) => String(value) === wanted)
    );
  }
  return false;
}

async function comments(transport, key) {
  const all = [];
  const ids = new Set();
  let total;
  for (let page = 0; page < 100; page++) {
    const startAt = all.length;
    const { payload } = await transport.request({
      path: `/rest/api/2/issue/${key}/comment`,
      query: { startAt, maxResults: 100 },
    });
    if (
      !Array.isArray(payload?.comments) ||
      !Number.isSafeInteger(payload.total) ||
      payload.total < 0 ||
      payload.startAt !== startAt ||
      (total !== undefined && total !== payload.total) ||
      startAt + payload.comments.length > payload.total ||
      payload.comments.some((c) => !/^\d+$/.test(String(c?.id ?? "")))
    )
      throw new JiraProtocolError("unsupported comment readback");
    total = payload.total;
    for (const comment of payload.comments) {
      const id = String(comment.id);
      if (ids.has(id))
        throw new JiraProtocolError("duplicate comment readback");
      ids.add(id);
      all.push(comment);
    }
    if (all.length >= payload.total) return all;
    if (!payload.comments.length) break;
  }
  throw new JiraProtocolError("incomplete comment readback");
}

export async function flagIssue(
  transport,
  { issueKey, action, comment, confirm }
) {
  const key = assertIssueKey(issueKey);
  if (!["add", "remove"].includes(action))
    throw new JiraProtocolError("action must be add or remove");
  const text = String(comment ?? "").trim();
  if (!text) throw new JiraProtocolError("comment is required");
  const flag = action === "add";
  const operation = { kind: "flag", issueKey: key, action, comment: text };
  const confirmation = confirmationDigest(operation);
  const plan = { operation, confirmation };
  if (confirm !== confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  const state = (value) => {
    if (!(value === null || Array.isArray(value)))
      throw new JiraProtocolError("unsupported flag readback");
    return Array.isArray(value) && value.length > 0;
  };
  const before = await getIssueState(transport, key, ["customfield_10641"]);
  if (state(before.fields.customfield_10641) === flag)
    throw new JiraProtocolError(
      "issue already has requested flag state; no comment posted"
    );
  const oldIds = new Set(
    (await comments(transport, key)).map((c) => String(c.id))
  );
  const { payload: html } = await transport.request({
    path: "/secure/GHBulkCommentDialog.jspa",
    query: { issueKeys: key },
    expect: "text",
  });
  if (typeof html !== "string")
    throw new JiraProtocolError("unsupported flag dialog");
  const tokens = defaultsFromEditHtml(html).get("formToken") ?? [];
  if (tokens.length !== 1 || !tokens[0])
    throw new JiraProtocolError("flag dialog has no unique formToken");
  const escaped = text.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]
  );
  const prefix = flag ? "(flag) Flag added" : "(flagoff) Flag removed";
  const body = `${prefix}\n\n${escaped}`;
  const { payload } = await transport.request({
    method: "POST",
    path: "/rest/greenhopper/1.0/xboard/issue/flag/flag.json",
    json: {
      issueKeys: [key],
      flag,
      comment: body,
      commentVisibility: "",
      formToken: tokens[0],
      temporaryAttachments: [],
    },
    mutation: true,
  });
  if (Array.isArray(payload?.errorMessages) && payload.errorMessages.length)
    throw new JiraAmbiguousOutcome(
      "flag response reported errors; verify before retrying"
    );
  try {
    const issue = await getIssueState(transport, key, ["customfield_10641"]);
    const after = await comments(transport, key);
    const found = after.find(
      (c) =>
        !oldIds.has(String(c.id)) &&
        (c.body === body || c.body === `${prefix}\n\n${text}`)
    );
    if (state(issue.fields.customfield_10641) !== flag || !found)
      throw new Error("readback mismatch");
    return {
      state: "Completed",
      ...plan,
      readback: { flag, commentId: String(found.id) },
    };
  } catch (error) {
    throw new JiraAmbiguousOutcome(
      "flag and new comment could not both be confirmed; verify before retrying",
      { cause: error }
    );
  }
}
