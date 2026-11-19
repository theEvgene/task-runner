import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import { createFormFromModel } from "./html-form.mjs";
import {
  CONTRACT,
  EPIC_NAME_FIELD,
  boardData,
  createRequest,
  getIssueState,
  persistedValueMatches,
  validStatusId,
} from "./operations-shared.mjs";

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
