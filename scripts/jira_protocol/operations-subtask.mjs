import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import { toHandoffEvidence } from "./handoff.mjs";
import {
  CONTRACT,
  assertIssueKey,
  createRequest,
  getIssueState,
  observingTransport,
  persistedValueMatches,
  resolveRestSelectValue,
  restFieldPayload,
  statusFor,
} from "./operations-shared.mjs";

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
