#!/usr/bin/env node
import { assertIssueKey } from "./operations.mjs";
import { JiraProtocolError } from "./protocol.mjs";

export const SUPPORTED_FIELDS = [
  "summary",
  "status",
  "assignee",
  "flagged",
  "parent",
  "subtasks",
];

export const FIELD_REST_MAP = Object.freeze({
  summary: "summary",
  status: "status",
  assignee: "assignee",
  flagged: "customfield_10641",
  parent: "parent",
  subtasks: "subtasks",
});

const FLAG_FIELD = "customfield_10641";

function incomplete(reason, details = {}) {
  return { status: "incomplete", reason, ...details };
}

function complete() {
  return { status: "complete" };
}

function categoryComplete(category) {
  return category?.status === "complete";
}

export function parseFieldList(raw) {
  const items = String(raw ?? "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  if (items.length === 0)
    throw new JiraProtocolError(
      `no fields requested; supported: ${SUPPORTED_FIELDS.join(", ")}`
    );
  const seen = [];
  for (const name of items) {
    if (!SUPPORTED_FIELDS.includes(name))
      throw new JiraProtocolError(
        `unknown field: ${name}; supported: ${SUPPORTED_FIELDS.join(", ")}`
      );
    if (!seen.includes(name)) seen.push(name);
  }
  return seen;
}

function restFieldsFor(requested) {
  return [...new Set(requested.map((name) => FIELD_REST_MAP[name]))];
}

function shapeSummary(value) {
  if (value === null || value === undefined)
    return { state: "empty", value: null };
  if (typeof value !== "string" || value.trim() === "")
    return { state: "empty", value: null };
  return { state: "value", value };
}

function shapeStatus(value) {
  if (value === null || value === undefined)
    return { state: "empty", value: null };
  if (typeof value !== "object" || Array.isArray(value))
    return {
      state: "unobserved",
      value: null,
      reason: "malformed_status_field",
    };
  if (typeof value.name !== "string")
    return {
      state: "unobserved",
      value: null,
      reason: "malformed_status_field",
    };
  return {
    state: "value",
    value: {
      name: value.name,
      id: value.id === undefined || value.id === null ? null : String(value.id),
    },
  };
}

function shapeAssignee(value) {
  if (value === null) return { state: "empty", value: null };
  if (value === undefined)
    return {
      state: "unobserved",
      value: null,
      reason: "field_unobserved_permission_or_configuration",
    };
  if (typeof value !== "object" || Array.isArray(value))
    return {
      state: "unobserved",
      value: null,
      reason: "malformed_assignee_field",
    };
  const login =
    typeof value.name === "string" && value.name.length > 0
      ? value.name
      : typeof value.key === "string" && value.key.length > 0
        ? value.key
        : null;
  const displayName =
    typeof value.displayName === "string" ? value.displayName : null;
  if (login === null && displayName === null)
    return {
      state: "unobserved",
      value: null,
      reason: "assignee_identity_unobserved",
    };
  return { state: "value", value: { login, displayName } };
}

function shapeFlagged(value) {
  if (value === undefined)
    return {
      state: "unobserved",
      value: null,
      reason: "field_unobserved_permission_or_configuration",
    };
  if (value === null) return { state: "value", value: false };
  if (Array.isArray(value)) return { state: "value", value: value.length > 0 };
  return {
    state: "unobserved",
    value: null,
    reason: "malformed_flag_field",
  };
}

function shapeParent(value) {
  if (value === undefined)
    return {
      state: "unobserved",
      value: null,
      reason: "field_unobserved_permission_or_configuration",
    };
  if (value === null) return { state: "empty", value: null };
  if (typeof value !== "object" || Array.isArray(value))
    return {
      state: "unobserved",
      value: null,
      reason: "malformed_parent_field",
    };
  if (typeof value.key !== "string")
    return {
      state: "unobserved",
      value: null,
      reason: "malformed_parent_field",
    };
  let key;
  try {
    key = assertIssueKey(value.key);
  } catch {
    return {
      state: "unobserved",
      value: null,
      reason: "invalid_or_out_of_scope_parent_key",
    };
  }
  const summary =
    value.fields && typeof value.fields === "object"
      ? (value.fields.summary ?? value.summary ?? null)
      : (value.summary ?? null);
  return {
    state: "value",
    value: {
      key,
      summary: typeof summary === "string" ? summary : null,
    },
  };
}

function shapeSubtasks(value) {
  if (value === undefined)
    return {
      state: "unobserved",
      value: null,
      reason: "field_unobserved_permission_or_configuration",
    };
  if (value === null) return { state: "empty", value: [] };
  if (!Array.isArray(value))
    return {
      state: "unobserved",
      value: null,
      reason: "malformed_child_list",
    };
  if (value.length === 0) return { state: "empty", value: [] };
  const items = [];
  const invalidIndexes = [];
  for (const [index, child] of value.entries()) {
    try {
      if (!child || typeof child !== "object") throw new Error("not an issue");
      const key = assertIssueKey(child.key);
      const fields =
        child.fields && typeof child.fields === "object" ? child.fields : {};
      const summary =
        typeof fields.summary === "string"
          ? fields.summary
          : typeof child.summary === "string"
            ? child.summary
            : null;
      const status =
        fields.status &&
        typeof fields.status === "object" &&
        !Array.isArray(fields.status) &&
        typeof fields.status.name === "string"
          ? fields.status.name
          : typeof child.status === "string"
            ? child.status
            : null;
      items.push({ key, summary, status });
    } catch {
      invalidIndexes.push(index);
    }
  }
  if (invalidIndexes.length > 0)
    return {
      state: "unobserved",
      value: items,
      reason: "malformed_child_entries",
      invalidIndexes,
    };
  return { state: "value", value: items };
}

function shapeField(name, restValue) {
  switch (name) {
    case "summary":
      return shapeSummary(restValue);
    case "status":
      return shapeStatus(restValue);
    case "assignee":
      return shapeAssignee(restValue);
    case "flagged":
      return shapeFlagged(restValue);
    case "parent":
      return shapeParent(restValue);
    case "subtasks":
      return shapeSubtasks(restValue);
    default:
      throw new JiraProtocolError(`unknown field: ${name}`);
  }
}

export async function readCompactFields(
  transport,
  { issueKey, fields: rawFields } = {}
) {
  const key = assertIssueKey(issueKey);
  const requested = parseFieldList(rawFields);
  const restFields = restFieldsFor(requested);
  const { payload } = await transport.request({
    path: `/rest/api/2/issue/${key}`,
    query: { fields: restFields.join(",") },
  });
  if (
    !payload ||
    typeof payload !== "object" ||
    payload.key !== key ||
    !payload.fields ||
    typeof payload.fields !== "object"
  )
    throw new JiraProtocolError("issue state did not match the requested key");
  const out = {};
  const categories = {};
  const visibilityLimits = [];
  for (const name of requested) {
    const restKey = FIELD_REST_MAP[name];
    const present = Object.hasOwn(payload.fields, restKey);
    const shaped = present
      ? shapeField(name, payload.fields[restKey])
      : {
          state: "unobserved",
          value: null,
          reason: "field_unobserved_permission_or_configuration",
        };
    if (shaped.state === "value" || shaped.state === "empty") {
      out[name] = { state: shaped.state, value: shaped.value };
      categories[name] = complete();
    } else {
      out[name] = { state: "unobserved", value: null };
      const reason =
        shaped.reason ?? "field_unobserved_permission_or_configuration";
      categories[name] = incomplete(reason, {
        ...(shaped.invalidIndexes
          ? { invalidIndexes: shaped.invalidIndexes }
          : {}),
      });
      visibilityLimits.push(`${name}:${reason}`);
    }
  }
  const comprehensive = Object.values(categories).every(categoryComplete);
  const restQuery = restFields.join(",");
  return {
    schema: "jira-protocol-result/2",
    operation: "read-fields",
    dataContract: {
      kind: "compact-selected-fields",
      comprehensive,
      fullContentProven: false,
      scope:
        "one LEL issue resource with only the requested caller-visible fields; no dedicated comments, no parent extraction, no attachment bodies",
    },
    key,
    requested,
    fields: out,
    commentsFetched: false,
    parentFetched: requested.includes("parent"),
    completeness: {
      comprehensive,
      categories,
      gaps: Object.entries(categories)
        .filter(([, value]) => !categoryComplete(value))
        .map(([category, value]) => ({ category, ...value })),
      visibilityLimits,
      truncated: false,
      omitted: [],
    },
    provenance: {
      issueResource: `/rest/api/2/issue/{key}?fields=${restQuery}`,
      commentsFetched: false,
      parentFetched: requested.includes("parent"),
      parentDepth: 0,
      attachmentBodiesFetched: false,
      wikiFetched: false,
      remoteLinksFetched: false,
    },
  };
}

export function formatForQueue(result, { maxBytes } = {}) {
  const limit =
    maxBytes === undefined ? Number.POSITIVE_INFINITY : Number(maxBytes);
  if (!Number.isSafeInteger(limit) && limit !== Number.POSITIVE_INFINITY)
    throw new JiraProtocolError("max-bytes must be a positive integer");
  if (limit !== Number.POSITIVE_INFINITY && limit <= 0)
    throw new JiraProtocolError("max-bytes must be a positive integer");
  const full = JSON.stringify(result);
  if (full.length <= limit) return { json: full, truncated: false };
  const skeleton = {
    ...result,
    fields: Object.fromEntries(
      (result.requested ?? []).map((name) => [
        name,
        { state: "truncated", value: null },
      ])
    ),
    dataContract: { ...result.dataContract, comprehensive: false },
    completeness: {
      comprehensive: false,
      categories: Object.fromEntries(
        (result.requested ?? []).map((name) => [
          name,
          incomplete("output_truncated", { maxBytes: limit }),
        ])
      ),
      gaps: (result.requested ?? []).map((name) => ({
        category: name,
        status: "incomplete",
        reason: "output_truncated",
        maxBytes: limit,
      })),
      visibilityLimits: result?.completeness?.visibilityLimits ?? [],
      truncated: true,
      omitted: [
        ...(result?.completeness?.omitted ?? []),
        `output-truncated-at-${limit}-bytes`,
      ],
    },
  };
  return { json: JSON.stringify(skeleton), truncated: true };
}

export { FLAG_FIELD };
