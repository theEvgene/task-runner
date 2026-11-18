import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import { toHandoffEvidence } from "./handoff.mjs";
import { createFormFromModel, defaultsFromEditHtml } from "./html-form.mjs";

export const CONTRACT = Object.freeze({
  origin: "https://jira3.tcsbank.ru",
  projectKey: "LEL",
  projectId: 10848,
  epicIssueTypeId: 10000,
  rapidViewId: 298290,
});

export const EPIC_NAME_FIELD = "customfield_10104";

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

export async function createEpic(
  transport,
  { summary, description, fields = {}, confirm }
) {
  const title = String(summary ?? "").trim();
  if (!title) throw new JiraProtocolError("summary is required");
  const suppliedFields = {
    ...fields,
    summary: title,
    // Jira enforces Epic Name on creation even when the Quick Create model
    // does not mark it required (issue #122). Mirror summary so plan and
    // submission always contain Epic Name equal to summary. No general
    // required-field discovery: only this known field is defaulted.
    [EPIC_NAME_FIELD]: title,
    ...(description !== undefined ? { description: String(description) } : {}),
  };
  const operation = {
    kind: "create-lel-backlog-epic",
    projectId: CONTRACT.projectId,
    issueTypeId: CONTRACT.epicIssueTypeId,
    fields: suppliedFields,
  };
  const { payload: model } = await createRequest(
    transport,
    "create_form_model",
    {
      method: "POST",
      path: "/secure/QuickCreateIssue!default.jspa",
      query: {
        decorator: "none",
        issuetype: CONTRACT.epicIssueTypeId,
        pid: CONTRACT.projectId,
      },
    }
  );
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
  const verified = await verifyCreatedEpic(
    transport,
    payload.issueKey,
    suppliedFields
  );
  return { ...plan, ...verified };
}

export async function verifyCreatedEpic(transport, issueKey, suppliedFields) {
  if (typeof issueKey !== "string" || !/^LEL-\d+$/.test(issueKey))
    throw new JiraProtocolError(
      "a valid LEL issue key is required for readback"
    );
  let issue;
  let board;
  try {
    const readbackFields = [
      "project",
      "issuetype",
      ...Object.keys(suppliedFields),
    ];
    [issue, board] = await Promise.all([
      getIssueState(transport, issueKey, readbackFields),
      boardData(transport),
    ]);
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
  const isOnBoard = board.issuesData.issues.some(
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
  );
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
  if (!isExpectedProject || !isEpic || !isOnBoard || !fieldsPersisted)
    throw new JiraAmbiguousOutcome(
      "created issue could not be confirmed with the requested persisted fields as an LEL Epic on the board",
      {
        issueKey,
        verification: { isExpectedProject, isEpic, isOnBoard, fieldsPersisted },
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
      onBoard: true,
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

function normalizeLabel(value) {
  return String(value ?? "")
    .replace(/[\t\n\f\r ]+/g, " ")
    .trim();
}

function resolveRestSelectValue(fieldMeta, fieldId, suppliedValue) {
  const value = String(suppliedValue);
  const allowed = Array.isArray(fieldMeta?.allowedValues)
    ? fieldMeta.allowedValues.filter(
        (option) => option && typeof option === "object"
      )
    : [];
  if (allowed.length === 0) return value;
  const exact = allowed.filter(
    (option) =>
      String(option.id ?? "") === value ||
      (option.value !== undefined && String(option.value) === value)
  );
  if (exact.length === 1) return exact[0];
  const wanted = normalizeLabel(value);
  const matched = allowed.filter(
    (option) =>
      normalizeLabel(option.name ?? "") === wanted ||
      normalizeLabel(option.value ?? "") === wanted ||
      normalizeLabel(option.key ?? "") === wanted
  );
  const unique = [...new Map(matched.map((option) => [option, option])).keys()];
  if (unique.length === 1) return unique[0];
  throw new JiraProtocolError(
    `select field ${fieldId} has an unsupported or ambiguous value`
  );
}

function restFieldPayload(fieldMeta, suppliedValue) {
  const allowed = Array.isArray(fieldMeta?.allowedValues)
    ? fieldMeta.allowedValues
    : [];
  if (allowed.length === 0) return String(suppliedValue);
  const resolved = resolveRestSelectValue(
    fieldMeta,
    fieldMeta?.id ?? "field",
    suppliedValue
  );
  if (typeof resolved === "object" && resolved !== null) {
    if (resolved.id !== undefined && resolved.value !== undefined)
      return { id: String(resolved.id), value: String(resolved.value) };
    if (resolved.id !== undefined) return { id: String(resolved.id) };
    if (resolved.value !== undefined) return { value: String(resolved.value) };
    if (resolved.name !== undefined) return { value: String(resolved.name) };
    return String(suppliedValue);
  }
  const match = allowed.find(
    (option) =>
      option &&
      typeof option === "object" &&
      (String(option.id ?? "") === String(resolved) ||
        (option.value !== undefined && String(option.value) === String(resolved)))
  );
  if (match) {
    if (match.id !== undefined && match.value !== undefined)
      return { id: String(match.id), value: String(match.value) };
    if (match.id !== undefined) return { id: String(match.id) };
    if (match.value !== undefined) return { value: String(match.value) };
  }
  return String(resolved);
}

export async function getSubtaskCreateMeta(transport, { issueTypeId } = {}) {
  const requested =
    issueTypeId === undefined || issueTypeId === null || String(issueTypeId).trim() === ""
      ? undefined
      : String(issueTypeId).trim();
  if (requested !== undefined && !/^[1-9]\d*$/.test(requested))
    throw new JiraProtocolError("issue-type-id must be a positive integer");
  const { payload } = await createRequest(transport, "subtask_meta", {
    path: "/rest/api/2/issue/createmeta",
    query: {
      projectKeys: CONTRACT.projectKey,
      expand: "projects.issuetypes.fields",
    },
  });
  const projects = payload?.projects;
  if (!Array.isArray(projects))
    throw new JiraProtocolError(
      "create metadata returned an unsupported body"
    );
  const project = projects.find(
    (candidate) =>
      candidate?.key === CONTRACT.projectKey &&
      String(candidate?.id) === String(CONTRACT.projectId)
  );
  if (!project)
    throw new JiraProtocolError(
      `create metadata does not declare project ${CONTRACT.projectKey}`
    );
  const issuetypes = project.issuetypes;
  if (!Array.isArray(issuetypes))
    throw new JiraProtocolError(
      "create metadata returned an unsupported issue-type body"
    );
  const subtasks = issuetypes.filter(
    (candidate) => candidate && candidate.subtask === true
  );
  if (subtasks.length === 0)
    throw new JiraProtocolError(
      `project ${CONTRACT.projectKey} exposes no subtask issue type in create metadata`
    );
  const availableSubtaskTypes = subtasks
    .filter(
      (candidate) =>
        candidate &&
        (candidate.id !== undefined &&
          candidate.id !== null &&
          String(candidate.id).trim() !== "")
    )
    .map((candidate) => ({
      id: String(candidate.id),
      name: String(candidate.name ?? ""),
    }))
    .sort((left, right) => left.id.localeCompare(right.id));
  let subtask;
  if (requested !== undefined) {
    subtask = subtasks.find((candidate) => String(candidate.id) === requested);
    if (!subtask)
      throw new JiraProtocolError(
        `requested issue-type-id ${requested} is not a live LEL subtask issue type; live subtask types: ${availableSubtaskTypes
          .map((candidate) => candidate.id)
          .join(", ") || "?"}`
      );
  } else {
    if (subtasks.length > 1)
      throw new JiraProtocolError(
        `project ${CONTRACT.projectKey} exposes multiple subtask issue types: ${availableSubtaskTypes
          .map((candidate) => candidate.id)
          .join(", ")} (pass --issue-type-id <id> to select one)`
      );
    subtask = subtasks[0];
  }
  if (
    subtask.id === undefined ||
    subtask.id === null ||
    String(subtask.id).trim() === ""
  )
    throw new JiraProtocolError(
      "create metadata subtask issue type has no id"
    );
  const fieldsMeta =
    subtask.fields && typeof subtask.fields === "object" ? subtask.fields : {};
  return {
    subtaskType: { id: String(subtask.id), name: String(subtask.name ?? "") },
    availableSubtaskTypes,
    fieldsMeta,
    modelFields: Object.entries(fieldsMeta)
      .map(([id, field]) => ({
        id: String(id),
        label: String(field?.name ?? id),
        required: Boolean(field?.required),
      }))
      .sort((left, right) => left.id.localeCompare(right.id)),
  };
}

export async function createSubtask(
  transport,
  { parent, summary, description, fields = {}, confirm, issueTypeId }
) {
  if (parent === undefined || parent === null || String(parent).trim() === "")
    throw new JiraProtocolError("parent is required");
  const parentKey = assertIssueKey(parent);
  const title = String(summary ?? "").trim();
  if (!title) throw new JiraProtocolError("summary is required");
  for (const reserved of ["parent", "project", "issuetype"]) {
    if (fields && Object.hasOwn(fields, reserved))
      throw new JiraProtocolError(
        `field '${reserved}' is managed by the subtask CLI and must not be supplied`
      );
  }
  const suppliedFields = {
    ...fields,
    summary: title,
    ...(description !== undefined ? { description: String(description) } : {}),
  };
  const observed = new Map();
  const watched = observingTransport(transport, observed);

  const buildPlan = async () => {
    const [meta, parentState] = await Promise.all([
      getSubtaskCreateMeta(watched, { issueTypeId }),
      createRequest(watched, "subtask_parent", {
        path: `/rest/api/2/issue/${parentKey}`,
        query: { fields: "project,issuetype" },
      }).then(({ payload }) => payload),
    ]);
    if (
      !parentState ||
      typeof parentState !== "object" ||
      parentState.key !== parentKey ||
      !parentState.fields ||
      typeof parentState.fields !== "object"
    )
      throw new JiraProtocolError("parent issue state did not match the key");
    const parentProject = parentState.fields.project;
    const isExpectedParentProject =
      parentProject?.key === CONTRACT.projectKey &&
      Number(parentProject?.id) === CONTRACT.projectId;
    if (!isExpectedParentProject)
      throw new JiraProtocolError("parent issue is not in the LEL project");
    const parentTypeId =
      parentState.fields.issuetype?.id !== undefined &&
      parentState.fields.issuetype?.id !== null
        ? String(parentState.fields.issuetype.id).trim()
        : "";
    if (!parentTypeId)
      throw new JiraProtocolError("parent issue does not declare an issue type id");
    if (
      parentTypeId === meta.subtaskType.id ||
      meta.availableSubtaskTypes.some((candidate) => candidate.id === parentTypeId)
    )
      throw new JiraProtocolError(
        "parent issue is a subtask; a subtask cannot have a child subtask"
      );
    const allowedFields = new Set([
      ...Object.keys(meta.fieldsMeta),
      "summary",
      "description",
    ]);
    const unknownFields = Object.keys(suppliedFields).filter(
      (field) => !allowedFields.has(field)
    );
    if (unknownFields.length)
      throw new JiraProtocolError(
        `fields are absent from the live create metadata: ${unknownFields.join(", ")}`
      );
    const requiredFields = Object.entries(meta.fieldsMeta)
      .filter(([id, field]) => field?.required && !["parent", "project", "issuetype"].includes(id))
      .map(([id, field]) => ({ id, label: String(field?.name ?? id) }));
    const missing = requiredFields.filter(({ id }) => {
      if (id === "parent" || id === "project" || id === "issuetype")
        return false;
      if (!Object.hasOwn(suppliedFields, id)) return true;
      return String(suppliedFields[id] ?? "").trim() === "";
    });
    const resolvedPayloadFields = {};
    for (const [fieldId, supplied] of Object.entries(suppliedFields)) {
      if (fieldId === "summary" || fieldId === "description") {
        resolvedPayloadFields[fieldId] = String(supplied);
        continue;
      }
      const fieldMeta = { ...meta.fieldsMeta[fieldId], id: fieldId };
      resolvedPayloadFields[fieldId] = restFieldPayload(fieldMeta, supplied);
    }
    const operation = {
      kind: "create-lel-subtask",
      projectId: CONTRACT.projectId,
      parentKey,
      issueTypeId: meta.subtaskType.id,
      fields: suppliedFields,
    };
    const submission = {
      path: "/rest/api/2/issue",
      fields: Object.fromEntries(
        Object.entries(resolvedPayloadFields).sort(([left], [right]) =>
          left.localeCompare(right)
        )
      ),
      modelFields: meta.modelFields,
      subtaskType: meta.subtaskType,
      parent: {
        key: parentKey,
        id:
          parentState.id !== undefined && parentState.id !== null
            ? String(parentState.id)
            : null,
        issuetypeId: parentTypeId || null,
      },
    };
    const confirmation = confirmationDigest({ operation, submission });
    return {
      operation,
      submission,
      confirmation,
      meta,
      parentState,
      resolvedPayloadFields,
      model: {
        requiredFields,
        missingFields: missing,
        subtaskType: meta.subtaskType,
        availableSubtaskTypes: meta.availableSubtaskTypes,
        parent: submission.parent,
      },
    };
  };

  const planned = await buildPlan();
  const plan = {
    operation: planned.operation,
    confirmation: planned.confirmation,
    model: planned.model,
  };
  if (planned.model.missingFields.length)
    return { state: "MissingRequiredFields", ...plan };
  if (confirm !== planned.confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  const refreshed = await buildPlan();
  if (refreshed.confirmation !== planned.confirmation)
    return {
      state: "AwaitingConfirmation",
      operation: refreshed.operation,
      confirmation: refreshed.confirmation,
      model: refreshed.model,
    };
  const createFields = {
    ...refreshed.resolvedPayloadFields,
    project: { id: String(CONTRACT.projectId) },
    parent: { key: parentKey },
    issuetype: { id: refreshed.meta.subtaskType.id },
  };
  const { payload } = await createRequest(watched, "subtask_submission", {
    method: "POST",
    path: "/rest/api/2/issue",
    json: { fields: createFields },
    mutation: true,
  });
  if (
    !payload ||
    typeof payload !== "object" ||
    !/^LEL-\d+$/.test(String(payload.key ?? ""))
  )
    throw new JiraAmbiguousOutcome(
      "subtask create did not return a confirmed LEL issue key"
    );
  const verified = await verifyCreatedSubtask(
    watched,
    payload.key,
    parentKey,
    refreshed.meta.subtaskType,
    suppliedFields
  );
  const handoff = [
    toHandoffEvidence({
      stage: "subtask_meta",
      method: "GET",
      path: "/rest/api/2/issue/createmeta",
      httpStatus: statusFor(observed, "GET", "/rest/api/2/issue/createmeta"),
    }),
    toHandoffEvidence({
      stage: "subtask_parent",
      method: "GET",
      path: `/rest/api/2/issue/${parentKey}`,
      httpStatus: statusFor(observed, "GET", `/rest/api/2/issue/${parentKey}`),
    }),
    toHandoffEvidence({
      stage: "subtask_submission",
      method: "POST",
      path: "/rest/api/2/issue",
      httpStatus: statusFor(observed, "POST", "/rest/api/2/issue"),
    }),
    toHandoffEvidence({
      stage: "subtask_readback",
      method: "GET",
      path: `/rest/api/2/issue/${payload.key}`,
      httpStatus: statusFor(
        observed,
        "GET",
        `/rest/api/2/issue/${payload.key}`
      ),
    }),
  ];
  return { ...plan, ...verified, handoff };
}

export async function verifyCreatedSubtask(
  transport,
  issueKey,
  parentKey,
  subtaskType,
  suppliedFields
) {
  if (typeof issueKey !== "string" || !/^LEL-\d+$/.test(issueKey))
    throw new JiraProtocolError("a valid LEL issue key is required for readback");
  const expectedParent = assertIssueKey(parentKey);
  const expectedTypeId = String(subtaskType?.id ?? "");
  if (!expectedTypeId)
    throw new JiraProtocolError("a subtask issue type id is required");
  let issue;
  try {
    const readbackFields = [
      "project",
      "issuetype",
      "parent",
      ...Object.keys(suppliedFields),
    ];
    issue = (
      await getIssueState(
        transport,
        issueKey,
        [...new Set(readbackFields)]
      )
    );
  } catch (error) {
    throw new JiraAmbiguousOutcome("created subtask readback failed", {
      cause: error,
      issueKey,
    });
  }
  const isExpectedProject =
    issue.fields.project?.key === CONTRACT.projectKey &&
    Number(issue.fields.project?.id) === CONTRACT.projectId;
  const isExpectedType =
    String(issue.fields.issuetype?.id) === expectedTypeId;
  const actualParentKey =
    issue.fields.parent && typeof issue.fields.parent === "object"
      ? issue.fields.parent.key
      : undefined;
  const isExpectedParent = actualParentKey === expectedParent;
  const persistedFields = Object.fromEntries(
    Object.entries(suppliedFields).map(([field, expected]) => {
      const actual = issue.fields[field];
      return [
        field,
        {
          expected,
          actual: actual ?? null,
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
    !isExpectedType ||
    !isExpectedParent ||
    !fieldsPersisted
  )
    throw new JiraAmbiguousOutcome(
      "created issue could not be confirmed as an LEL subtask of the requested parent",
      {
        issueKey,
        verification: {
          isExpectedProject,
          isExpectedType,
          isExpectedParent,
          fieldsPersisted,
        },
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
      parentKey: actualParentKey,
      parentId:
        issue.fields.parent?.id !== undefined &&
        issue.fields.parent?.id !== null
          ? String(issue.fields.parent.id)
          : null,
      persistedFields,
    },
  };
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
