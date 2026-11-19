import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import {
  CONTRACT,
  assertIssueKey,
  getIssueState,
  getSubtaskCreateMeta,
} from "./operations.mjs";
import { BATCH_MAX_ITEMS, normalizeSummary } from "./batch-parse.mjs";

async function existingSubtasks(transport, parentKey) {
  let state;
  try {
    state = await getIssueState(transport, parentKey, [
      "project",
      "issuetype",
      "subtasks",
    ]);
  } catch (error) {
    if (
      error instanceof JiraProtocolError ||
      error instanceof JiraAmbiguousOutcome
    )
      error.stage = "batch_existing";
    throw error;
  }
  const listed = state.fields?.subtasks;
  if (!Array.isArray(listed))
    throw Object.assign(
      new JiraProtocolError(
        "parent subtask list is unavailable; existing matches are unverified"
      ),
      { stage: "batch_existing" }
    );
  return {
    state,
    existing: listed.map((entry, position) => {
      const key =
        entry && typeof entry === "object" ? String(entry.key ?? "") : "";
      const summary =
        entry &&
        typeof entry === "object" &&
        entry.fields &&
        typeof entry.fields === "object"
          ? entry.fields.summary
          : undefined;
      if (!/^LEL-\d+$/.test(key) || typeof summary !== "string")
        throw Object.assign(
          new JiraProtocolError(
            `parent subtask list entry ${position + 1} is unsupported; existing matches are unverified`
          ),
          { stage: "batch_existing" }
        );
      return { key, summary, normalized: normalizeSummary(summary) };
    }),
  };
}

// Read-only batch plan. Every item is matched against the live children of
// the parent: exactly one normalized-summary hit is an idempotent skip,
// several hits (or several identical file entries) need an explicit
// --allow-duplicate decision, and only unmatched items are written after
// confirmation. No write happens in planning.
export async function planSubtaskBatch(
  transport,
  { parent, items, issueTypeId, allowDuplicates = [] }
) {
  const parentKey = assertIssueKey(parent);
  if (!Array.isArray(items) || items.length === 0)
    throw new JiraProtocolError("batch holds no subtasks");
  if (items.length > BATCH_MAX_ITEMS)
    throw new JiraProtocolError(
      `batch holds more than ${BATCH_MAX_ITEMS} subtasks`
    );
  const allowed = new Set(
    (Array.isArray(allowDuplicates) ? allowDuplicates : [allowDuplicates])
      .filter((value) => value !== undefined)
      .map((value) => normalizeSummary(value))
      .filter(Boolean)
  );
  const counts = new Map();
  for (const item of items) {
    counts.set(item.normalized, (counts.get(item.normalized) ?? 0) + 1);
  }
  const [meta, { state: parentState, existing }] = await Promise.all([
    getSubtaskCreateMeta(transport, { issueTypeId }),
    existingSubtasks(transport, parentKey),
  ]);
  const parentProject = parentState.fields?.project;
  if (
    parentProject?.key !== CONTRACT.projectKey ||
    Number(parentProject?.id) !== CONTRACT.projectId
  )
    throw new JiraProtocolError("parent issue is not in the LEL project");
  const parentTypeId = String(parentState.fields?.issuetype?.id ?? "").trim();
  if (!parentTypeId)
    throw new JiraProtocolError(
      "parent issue does not declare an issue type id"
    );
  if (
    parentTypeId === meta.subtaskType.id ||
    meta.availableSubtaskTypes.some(
      (candidate) => candidate.id === parentTypeId
    )
  )
    throw new JiraProtocolError(
      "parent issue is a subtask; a subtask cannot have a child subtask"
    );
  const required = Object.entries(meta.fieldsMeta ?? {})
    .filter(
      ([id, field]) =>
        field?.required && !["parent", "project", "issuetype"].includes(id)
    )
    .map(([id, field]) => ({ id, label: String(field?.name ?? id) }));
  const planned = items.map((item, position) => {
    const supplied = item.fields ?? {};
    const missing = required.filter(({ id }) => {
      if (id === "summary") return false;
      if (!Object.hasOwn(supplied, id)) return true;
      return String(supplied[id] ?? "").trim() === "";
    });
    const hits = existing.filter(
      (entry) => entry.normalized === item.normalized
    );
    let match = "new";
    if (counts.get(item.normalized) > 1) match = "ambiguous";
    else if (hits.length === 1)
      match = allowed.has(item.normalized) ? "duplicate-allowed" : "exists";
    else if (hits.length > 1)
      match = allowed.has(item.normalized) ? "duplicate-allowed" : "ambiguous";
    return {
      index: position + 1,
      summary: item.summary,
      ...(item.description !== undefined
        ? { description: item.description }
        : {}),
      fields: supplied,
      match,
      existingKeys: hits.map((entry) => entry.key),
      missingFields: missing,
    };
  });
  const operation = {
    kind: "create-lel-subtasks",
    parentKey,
    issueTypeId: meta.subtaskType.id,
    itemCount: planned.length,
    allowDuplicates: [...allowed].sort(),
  };
  const model = {
    requiredFields: required,
    subtaskType: meta.subtaskType,
    availableSubtaskTypes: meta.availableSubtaskTypes,
    parent: {
      key: parentKey,
      id:
        parentState.id !== undefined && parentState.id !== null
          ? String(parentState.id)
          : null,
      issuetypeId: parentTypeId,
    },
  };
  const confirmation = confirmationDigest({
    operation,
    items: planned.map((item) => ({
      summary: item.summary,
      ...(item.description !== undefined
        ? { description: item.description }
        : {}),
      fields: item.fields,
    })),
    existing: existing.map((entry) => ({
      key: entry.key,
      summary: entry.summary,
    })),
    subtaskType: meta.subtaskType,
  });
  const receipt = {
    operation,
    confirmation,
    model,
    existing,
    items: planned,
  };
  if (planned.some((item) => item.missingFields.length > 0))
    return { state: "MissingRequiredFields", ...receipt };
  if (planned.every((item) => item.match === "exists"))
    return {
      state: "Completed",
      ...receipt,
      results: planned.map((item) => ({
        index: item.index,
        summary: item.summary,
        state: "AlreadyExists",
        key: item.existingKeys[0],
        link: `${CONTRACT.origin}/browse/${item.existingKeys[0]}`,
      })),
      writes: 0,
      handoff: [],
    };
  return { state: "AwaitingConfirmation", ...receipt };
}

export function blockedItems(planned) {
  return planned
    .filter((item) => item.match === "ambiguous")
    .map((item) => ({
      index: item.index,
      summary: item.summary,
      existingKeys: item.existingKeys,
    }));
}
