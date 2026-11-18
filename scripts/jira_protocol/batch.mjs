import { readFile } from "node:fs/promises";
import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import {
  CONTRACT,
  assertIssueKey,
  createSubtask,
  getIssueState,
  getSubtaskCreateMeta,
} from "./operations.mjs";
import { toHandoffEvidence } from "./handoff.mjs";

export const BATCH_MAX_ITEMS = 50;

export function normalizeSummary(value) {
  return String(value ?? "")
    .replace(/[\t\n\f\r ]+/g, " ")
    .trim();
}

function coerceFields(fields, index) {
  if (fields === undefined) return {};
  if (!fields || typeof fields !== "object" || Array.isArray(fields))
    throw new JiraProtocolError(`batch item ${index}: fields must be an object`);
  const coerced = {};
  for (const [key, value] of Object.entries(fields)) {
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean")
      throw new JiraProtocolError(
        `batch item ${index}: field ${key} must be a string`
      );
    coerced[key] = String(value);
  }
  return coerced;
}

// Batch file format: a UTF-8 JSON array whose entries carry a summary, an
// optional multiline description, and optional custom fields. Multiline and
// non-ASCII text travel as JSON strings, so no line-based format is needed.
export function parseBatchFile(text) {
  let parsed;
  try {
    parsed = JSON.parse(String(text ?? ""));
  } catch {
    throw new JiraProtocolError("batch file is not valid JSON");
  }
  if (!Array.isArray(parsed))
    throw new JiraProtocolError("batch file must hold a JSON array of subtasks");
  if (parsed.length === 0)
    throw new JiraProtocolError("batch file holds no subtasks");
  if (parsed.length > BATCH_MAX_ITEMS)
    throw new JiraProtocolError(
      `batch file holds more than ${BATCH_MAX_ITEMS} subtasks`
    );
  return parsed.map((entry, position) => {
    const index = position + 1;
    if (!entry || typeof entry !== "object" || Array.isArray(entry))
      throw new JiraProtocolError(`batch item ${index} must be an object`);
    const summary = String(entry.summary ?? "").trim();
    if (!summary)
      throw new JiraProtocolError(`batch item ${index}: summary is required`);
    const item = { summary, normalized: normalizeSummary(summary) };
    if (!item.normalized)
      throw new JiraProtocolError(`batch item ${index}: summary is required`);
    if (entry.description !== undefined)
      item.description = String(entry.description);
    item.fields = coerceFields(entry.fields, index);
    return item;
  });
}

export async function loadBatchFile(path) {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    throw new JiraProtocolError("--file is unreadable", {
      details: { path, ...(error?.code ? { code: error.code } : {}) },
    });
  }
}

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
        entry && typeof entry === "object" && entry.fields && typeof entry.fields === "object"
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
    throw new JiraProtocolError("parent issue does not declare an issue type id");
  if (
    parentTypeId === meta.subtaskType.id ||
    meta.availableSubtaskTypes.some((candidate) => candidate.id === parentTypeId)
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
    const hits = existing.filter((entry) => entry.normalized === item.normalized);
    let match = "new";
    if (counts.get(item.normalized) > 1) match = "ambiguous";
    else if (hits.length === 1)
      match = allowed.has(item.normalized) ? "duplicate-allowed" : "exists";
    else if (hits.length > 1)
      match = allowed.has(item.normalized) ? "duplicate-allowed" : "ambiguous";
    return {
      index: position + 1,
      summary: item.summary,
      ...(item.description !== undefined ? { description: item.description } : {}),
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
      ...(item.description !== undefined ? { description: item.description } : {}),
      fields: item.fields,
    })),
    existing: existing.map((entry) => ({ key: entry.key, summary: entry.summary })),
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

function blockedItems(planned) {
  return planned
    .filter((item) => item.match === "ambiguous")
    .map((item) => ({
      index: item.index,
      summary: item.summary,
      existingKeys: item.existingKeys,
    }));
}

// Confirmed batch executor. Re-verifies metadata, parent, and the existing
// children before any write; a changed digest restarts confirmation. Each
// item reuses the single-subtask two-phase operation. An unknown item
// outcome stops that item only and never retries it; HTTP 401/403 aborts
// the batch as terminal for the route.
export async function createSubtaskBatch(
  transport,
  { parent, items, issueTypeId, allowDuplicates, confirm }
) {
  const planned = await planSubtaskBatch(transport, {
    parent,
    items,
    issueTypeId,
    allowDuplicates,
  });
  if (planned.state === "Completed") return planned;
  if (planned.state === "MissingRequiredFields") return planned;
  if (confirm !== planned.confirmation)
    return { state: "AwaitingConfirmation", ...planned };
  const blocked = blockedItems(planned.items);
  if (blocked.length)
    throw new JiraProtocolError(
      "ambiguous subtask matches need an explicit --allow-duplicate decision",
      { details: { blocked } }
    );
  const actionable = planned.items.filter((item) => item.match !== "exists");
  const results = [];
  const handoff = [];
  let unknown = 0;
  let failed = 0;
  for (const item of actionable) {
    const input = {
      parent: planned.operation.parentKey,
      summary: item.summary,
      ...(item.description !== undefined ? { description: item.description } : {}),
      fields: item.fields,
      issueTypeId: planned.operation.issueTypeId,
    };
    try {
      const preview = await createSubtask(transport, input);
      if (preview.state !== "AwaitingConfirmation") {
        failed += 1;
        results.push({
          index: item.index,
          summary: item.summary,
          state: "Failed",
          error: `single-subtask plan returned ${preview.state}`,
        });
        continue;
      }
      const done = await createSubtask(transport, {
        ...input,
        confirm: preview.confirmation,
      });
      if (done.state !== "Completed") {
        failed += 1;
        results.push({
          index: item.index,
          summary: item.summary,
          state: "Failed",
          error: `single-subtask write returned ${done.state}; rerun the batch plan`,
        });
        continue;
      }
      results.push({
        index: item.index,
        summary: item.summary,
        state: "Created",
        key: done.issue.key,
        link: `${CONTRACT.origin}/browse/${done.issue.key}`,
      });
      handoff.push(
        toHandoffEvidence({
          stage: "batch_item_submission",
          method: "POST",
          path: "/rest/api/2/issue",
        }),
        toHandoffEvidence({
          stage: "batch_item_readback",
          method: "GET",
          path: `/rest/api/2/issue/${done.issue.key}`,
        })
      );
    } catch (error) {
      if (error instanceof JiraAmbiguousOutcome) {
        unknown += 1;
        results.push({
          index: item.index,
          summary: item.summary,
          state: "UnknownOutcome",
          ...(typeof error.issueKey === "string" ? { key: error.issueKey } : {}),
          ...(error.verification ? { verification: error.verification } : {}),
        });
        handoff.push(
          toHandoffEvidence({
            stage: "batch_item_submission",
            method: "POST",
            path: "/rest/api/2/issue",
            note: "outcome unknown; no automatic retry",
          })
        );
        continue;
      }
      if (error instanceof JiraProtocolError && (error.status === 401 || error.status === 403))
        throw error;
      failed += 1;
      results.push({
        index: item.index,
        summary: item.summary,
        state: "Failed",
        error: error instanceof Error ? error.message : "unknown error",
      });
    }
  }
  for (const item of planned.items.filter((entry) => entry.match === "exists")) {
    results.push({
      index: item.index,
      summary: item.summary,
      state: "AlreadyExists",
      key: item.existingKeys[0],
      link: `${CONTRACT.origin}/browse/${item.existingKeys[0]}`,
    });
  }
  results.sort((left, right) => left.index - right.index);
  const receipt = {
    operation: planned.operation,
    confirmation: planned.confirmation,
    model: planned.model,
    existing: planned.existing,
    items: planned.items,
    results,
    writes: results.filter((entry) => entry.state === "Created").length,
    handoff,
  };
  if (unknown > 0) return { state: "UnknownOutcome", ...receipt };
  if (failed > 0) return { state: "Failed", ...receipt };
  return { state: "Completed", ...receipt };
}
