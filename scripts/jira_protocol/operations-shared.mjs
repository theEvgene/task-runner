import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
} from "./protocol.mjs";

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

export function integer(value, name) {
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

export async function boardData(transport) {
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

export function validStatusId(value) {
  return typeof value === "string"
    ? /^[1-9]\d*$/.test(value)
    : Number.isSafeInteger(value) && value > 0;
}

export async function createRequest(transport, stage, request) {
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

export function observedStatus(result) {
  const raw = Number(result?.response?.status);
  return Number.isSafeInteger(raw) && raw >= 100 && raw <= 599 ? raw : "unknown";
}

export function observingTransport(transport, statuses) {
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

export function statusFor(statuses, method, path) {
  return statuses.get(`${method} ${path}`) ?? "unknown";
}

export function persistedValueMatches(actual, expected) {
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

export function normalizeLabel(value) {
  return String(value ?? "")
    .replace(/[\t\n\f\r ]+/g, " ")
    .trim();
}

export function resolveRestSelectValue(fieldMeta, fieldId, suppliedValue) {
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

export function restFieldPayload(fieldMeta, suppliedValue) {
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

export async function comments(transport, key) {
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
