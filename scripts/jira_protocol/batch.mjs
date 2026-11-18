import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
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
export const BATCH_JOURNAL_SCHEMA = "jira-subtasks-journal/1";

// Summary matching is whitespace-normalized only: NFC plus collapsing every
// Unicode whitespace run to one ASCII space, then trimming. Matching stays
// case-sensitive: "Fix" and "fix" are different items. Two file entries (or a
// file entry and a live child) that normalize to the same string are treated
// as the same match candidate and need an explicit --allow-duplicate decision
// to proceed at all; identical file entries (same normalized summary twice
// in the batch file) stay ambiguous even when allowed and are never written,
// so a repeated title in one batch cannot silently create twins. The receipt
// keeps both the raw summary and the normalized form.
export function normalizeSummary(value) {
  return String(value ?? "")
    .normalize("NFC")
    .replace(/\s+/gu, " ")
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
    const summary = String(entry.summary ?? "")
      .normalize("NFC")
      .trim();
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

// Stable batch identity bound to the requested inputs only (parent, file
// snapshot, requested type, allow-duplicate set). Live discovery (existing
// children, resolved type) is verified separately so a changed live model
// fails closed instead of silently resuming a different batch.
export function batchIdentity({ parent, items, issueTypeId, allowDuplicates }) {
  const parentKey = String(parent ?? "").trim().toUpperCase();
  const requested =
    issueTypeId === undefined ||
    issueTypeId === null ||
    String(issueTypeId).trim() === ""
      ? null
      : String(issueTypeId).trim();
  const allowed = [
    ...new Set(
      (Array.isArray(allowDuplicates) ? allowDuplicates : [allowDuplicates])
        .filter((value) => value !== undefined)
        .map((value) => normalizeSummary(value))
        .filter(Boolean)
    ),
  ].sort();
  const snapshot = (Array.isArray(items) ? items : []).map((item) => ({
    summary: item.summary,
    ...(item.description !== undefined ? { description: item.description } : {}),
    fields: item.fields ?? {},
  }));
  return confirmationDigest({
    kind: "create-lel-subtasks-batch",
    parentKey,
    requestedIssueTypeId: requested,
    items: snapshot,
    allowDuplicates: allowed,
  });
}

export function parseResolveUnknown(list) {
  const entries = Array.isArray(list) ? list : [list];
  const resolved = new Map();
  for (const raw of entries) {
    if (raw === undefined) continue;
    const text = String(raw).trim();
    if (!text) continue;
    const match = /^(\d+)\s*[:=]\s*(retry|LEL-\d+)$/i.exec(text);
    if (!match)
      throw new JiraProtocolError(
        "--resolve-unknown must use <index>=retry or <index>=LEL-<n>"
      );
    const index = Number(match[1]);
    if (!Number.isSafeInteger(index) || index < 1 || index > BATCH_MAX_ITEMS)
      throw new JiraProtocolError("--resolve-unknown index is out of range");
    if (resolved.has(index))
      throw new JiraProtocolError("--resolve-unknown lists an index twice");
    const decision =
      match[2].toLowerCase() === "retry"
        ? { mode: "retry" }
        : { mode: "exists", key: match[2].toUpperCase() };
    resolved.set(index, decision);
  }
  return resolved;
}

export async function loadBatchJournal(path) {
  let text;
  try {
    text = await readFile(path, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw Object.assign(
      new JiraProtocolError("--journal is unreadable", {
        details: { path, ...(error?.code ? { code: error.code } : {}) },
      }),
      { stage: "batch_journal" }
    );
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw Object.assign(
      new JiraProtocolError("--journal holds invalid JSON", {
        details: { path },
      }),
      { stage: "batch_journal" }
    );
  }
  if (!parsed || typeof parsed !== "object" || parsed.schema !== BATCH_JOURNAL_SCHEMA)
    throw Object.assign(
      new JiraProtocolError("--journal schema is unsupported", {
        details: { path },
      }),
      { stage: "batch_journal" }
    );
  return parsed;
}

export async function saveBatchJournal(path, data) {
  try {
    const payload = `${JSON.stringify(data, null, 2)}\n`;
    await mkdir(dirname(path), { recursive: true });
    const tmp = `${path}.tmp.${process.pid}`;
    await writeFile(tmp, payload, "utf8");
    await rename(tmp, path);
  } catch (error) {
    if (error instanceof JiraProtocolError && error.stage === "batch_journal") throw error;
    throw Object.assign(
      new JiraProtocolError("--journal write failed", {
        details: { path, ...(error?.code ? { code: error.code } : {}) },
      }),
      { stage: "batch_journal" }
    );
  }
}

function journalResultFor(journal, index) {
  const bucket = journal?.results;
  if (!bucket || typeof bucket !== "object") return undefined;
  return bucket[String(index)];
}

function journalUnknownFor(journal, index) {
  const bucket = journal?.unknown;
  if (!bucket || typeof bucket !== "object") return undefined;
  return bucket[String(index)];
}

function journalInflightFor(journal, index) {
  const bucket = journal?.inflight;
  if (!bucket || typeof bucket !== "object") return undefined;
  return bucket[String(index)];
}

function journalBlockedIndexes(journal) {
  const blocked = new Set();
  for (const bucket of [journal?.unknown, journal?.inflight]) {
    if (!bucket || typeof bucket !== "object") continue;
    for (const key of Object.keys(bucket)) {
      const index = Number(key);
      if (Number.isSafeInteger(index)) blocked.add(index);
    }
  }
  return [...blocked].sort((a, b) => a - b);
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
// the batch as terminal for the route. When journalPath is supplied, a
// durable in-flight marker is written atomically BEFORE each possible
// mutation, every confirmed per-item result replaces that marker before the
// next item starts, an interrupted run resumes by skipping journaled
// successes, and a journaled in-flight or UnknownOutcome entry blocks any
// rerun of that index until it is explicitly resolved with
// --resolve-unknown <index>=retry|LEL-<n> after operator verification.
// Only failures proven to have happened before dispatch (plan validation,
// meta/parent read failures, or a definitive 4xx submission rejection) may
// clear the marker as Failed; any uncertain failure keeps a blocking marker
// and never retries blindly.
export async function createSubtaskBatch(
  transport,
  {
    parent,
    items,
    issueTypeId,
    allowDuplicates,
    confirm,
    journalPath,
    resolveUnknown,
  }
) {
  const batchId = batchIdentity({ parent, items, issueTypeId, allowDuplicates });
  const resolvedDecisions = parseResolveUnknown(
    resolveUnknown === undefined ? [] : resolveUnknown
  );
  let journal = null;
  if (journalPath) {
    journal = await loadBatchJournal(journalPath);
    if (journal && journal.batchId !== batchId)
      throw new JiraProtocolError(
        "journal belongs to a different batch; use a fresh --journal path or the matching batch file",
        { details: { journalPath, batchId, journalBatchId: journal.batchId } }
      );
    if (journal) {
      if (!journal.results || typeof journal.results !== "object") journal.results = {};
      if (!journal.unknown || typeof journal.unknown !== "object") journal.unknown = {};
      if (!journal.inflight || typeof journal.inflight !== "object") journal.inflight = {};
      // Apply explicit operator resolutions before any write. A resolution
      // applies to either a journaled UnknownOutcome or a leftover in-flight
      // marker from an interrupted run; both need operator verification.
      let mutated = false;
      for (const [index, decision] of resolvedDecisions) {
        const pending = journalUnknownFor(journal, index) ?? journalInflightFor(journal, index);
        if (!pending) continue;
        if (decision.mode === "retry") {
          delete journal.unknown[String(index)];
          delete journal.inflight[String(index)];
          mutated = true;
        } else {
          journal.results[String(index)] = {
            index,
            summary: pending.summary,
            state: "Created",
            key: decision.key,
            link: `${CONTRACT.origin}/browse/${decision.key}`,
            resolvedFromUnknown: true,
          };
          delete journal.unknown[String(index)];
          delete journal.inflight[String(index)];
          mutated = true;
        }
      }
      if (mutated) {
        journal.updatedAt = new Date().toISOString();
        await saveBatchJournal(journalPath, journal);
      }
    }
  }
  const planned = await planSubtaskBatch(transport, {
    parent,
    items,
    issueTypeId,
    allowDuplicates,
  });
  const journalView = journalPath
    ? {
        path: journalPath,
        batchId,
        resumed: journal
          ? Object.keys(journal.results ?? {})
              .map(Number)
              .sort((a, b) => a - b)
          : [],
        blockedUnknown: journal
          ? journalBlockedIndexes(journal).filter(
              (index) =>
                !resolvedDecisions.has(index) ||
                journalUnknownFor(journal, index) ||
                journalInflightFor(journal, index)
            )
          : [],
        resolvedIssueTypeId: journal?.resolvedIssueTypeId ?? null,
      }
    : undefined;
  // Verify the journal binds the same live subtask type once planning has
  // resolved it; a changed live model fails closed instead of resuming.
  if (journal && journal.resolvedIssueTypeId !== undefined && journal.resolvedIssueTypeId !== null) {
    if (String(journal.resolvedIssueTypeId) !== String(planned.operation?.issueTypeId ?? "")) {
      throw new JiraProtocolError(
        "journal subtask type no longer matches live metadata; re-plan with a fresh --journal path",
        {
          details: {
            journalPath,
            batchId,
            journalType: journal.resolvedIssueTypeId,
            liveType: planned.operation?.issueTypeId,
          },
        }
      );
    }
  }
  if (planned.state === "Completed") {
    if (!journalView) return planned;
    return { ...planned, journal: journalView };
  }
  if (planned.state === "MissingRequiredFields") {
    if (!journalView) return planned;
    return { ...planned, journal: journalView };
  }
  if (confirm !== planned.confirmation) {
    if (!journalView) return { state: "AwaitingConfirmation", ...planned };
    return { state: "AwaitingConfirmation", ...planned, journal: journalView };
  }
  const blocked = blockedItems(planned.items);
  if (blocked.length)
    throw new JiraProtocolError(
      "ambiguous subtask matches need an explicit --allow-duplicate decision",
      { details: { blocked } }
    );
  if (journal) {
    const stillBlocked = journalBlockedIndexes(journal);
    if (stillBlocked.length)
      throw new JiraProtocolError(
        "batch has journaled UnknownOutcome or in-flight entries; resolve each with --resolve-unknown <index>=retry|LEL-<n> after verification before any rerun, no blind repeat",
        { details: { blockedUnknown: stillBlocked, journalPath, batchId } }
      );
  }
  // Claim the journal before the first write so a crash after this point
  // resumes from a bound identity instead of starting over.
  if (journalPath && !journal) {
    journal = {
      schema: BATCH_JOURNAL_SCHEMA,
      batchId,
      parentKey: planned.operation.parentKey,
      requestedIssueTypeId:
        issueTypeId === undefined ||
        issueTypeId === null ||
        String(issueTypeId).trim() === ""
          ? null
          : String(issueTypeId).trim(),
      resolvedIssueTypeId: planned.operation.issueTypeId,
      itemCount: planned.items.length,
      items: planned.items.map((entry) => ({
        index: entry.index,
        summary: entry.summary,
        normalized: normalizeSummary(entry.summary),
      })),
      results: {},
      unknown: {},
      inflight: {},
      updatedAt: new Date().toISOString(),
    };
    await saveBatchJournal(journalPath, journal);
  } else if (journal && !journal.resolvedIssueTypeId) {
    journal.resolvedIssueTypeId = planned.operation.issueTypeId;
    if (!journal.results || typeof journal.results !== "object") journal.results = {};
    if (!journal.unknown || typeof journal.unknown !== "object") journal.unknown = {};
    if (!journal.inflight || typeof journal.inflight !== "object") journal.inflight = {};
    journal.updatedAt = new Date().toISOString();
    await saveBatchJournal(journalPath, journal);
  } else if (journal && (!journal.inflight || typeof journal.inflight !== "object")) {
    journal.inflight = {};
    journal.updatedAt = new Date().toISOString();
    await saveBatchJournal(journalPath, journal);
  }
  const actionable = planned.items.filter((item) => item.match !== "exists");
  const results = [];
  const handoff = [];
  let unknown = 0;
  let failed = 0;
  let runWrites = 0;
  // A failure is proven to have happened before dispatch only when no
  // mutation could have started: input validation with no stage, a
  // meta/parent read failure, or a definitive 4xx submission rejection.
  // Anything else (missing stage on a generic error, an unexpected stage,
  // or a submission without a definitive rejection status) stays blocking
  // and is never misclassified as a safe Failed retry.
  const isProvenPreDispatchFailure = (error) => {
    if (!(error instanceof JiraProtocolError)) return false;
    if (error.stage === undefined || error.stage === null) return true;
    if (
      error.stage === "subtask_meta" ||
      error.stage === "subtask_parent" ||
      error.stage === "batch_existing" ||
      error.stage === "batch_journal"
    )
      return error.stage !== "batch_journal";
    if (error.stage === "subtask_submission") {
      if (error.status === undefined || error.status === null) return false;
      return Number(error.status) >= 400 && Number(error.status) < 500;
    }
    return false;
  };
  const persistInflight = async (index, entry) => {
    if (!journalPath) return;
    if (!journal.inflight || typeof journal.inflight !== "object") journal.inflight = {};
    journal.inflight[String(index)] = entry;
    journal.updatedAt = new Date().toISOString();
    await saveBatchJournal(journalPath, journal);
  };
  const clearInflight = async (index) => {
    if (!journalPath) return;
    if (journal?.inflight && Object.hasOwn(journal.inflight, String(index))) {
      delete journal.inflight[String(index)];
      journal.updatedAt = new Date().toISOString();
      await saveBatchJournal(journalPath, journal);
    }
  };
  const persistSuccess = async (index, entry) => {
    if (!journalPath) return;
    delete journal.inflight?.[String(index)];
    journal.results[String(index)] = entry;
    journal.updatedAt = new Date().toISOString();
    await saveBatchJournal(journalPath, journal);
  };
  const persistUnknown = async (index, entry) => {
    if (!journalPath) return;
    delete journal.inflight?.[String(index)];
    journal.unknown[String(index)] = entry;
    journal.updatedAt = new Date().toISOString();
    await saveBatchJournal(journalPath, journal);
  };
  // Resume: journaled successes are reported without a new write and emit no
  // new submission evidence for this run.
  for (const item of actionable) {
    const journaled = journal ? journalResultFor(journal, item.index) : undefined;
    if (journaled && (journaled.state === "Created" || journaled.state === "AlreadyExists")) {
      results.push(journaled);
      continue;
    }
    const input = {
      parent: planned.operation.parentKey,
      summary: item.summary,
      ...(item.description !== undefined ? { description: item.description } : {}),
      fields: item.fields,
      issueTypeId: planned.operation.issueTypeId,
    };
    // Durable in-flight marker BEFORE any possible mutation: a crash after
    // the remote POST succeeds but before persistSuccess leaves this marker,
    // and a stale child listing cannot cause a blind duplicate because the
    // next run blocks on the marker until explicit verified resolution.
    if (journalPath) {
      await persistInflight(item.index, {
        index: item.index,
        summary: item.summary,
        normalized: normalizeSummary(item.summary),
        startedAt: new Date().toISOString(),
      });
    }
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
        await clearInflight(item.index);
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
        await clearInflight(item.index);
        continue;
      }
      const entry = {
        index: item.index,
        summary: item.summary,
        state: "Created",
        key: done.issue.key,
        link: `${CONTRACT.origin}/browse/${done.issue.key}`,
      };
      results.push(entry);
      runWrites += 1;
      await persistSuccess(item.index, entry);
      const submissionStatus =
        (Array.isArray(done.handoff) &&
          done.handoff.find((e) => e.stage === "subtask_submission")?.httpStatus) ??
        "unknown";
      const readbackStatus =
        (Array.isArray(done.handoff) &&
          done.handoff.find((e) => e.stage === "subtask_readback")?.httpStatus) ??
        "unknown";
      handoff.push(
        toHandoffEvidence({
          stage: "batch_item_submission",
          method: "POST",
          path: "/rest/api/2/issue",
          httpStatus: submissionStatus,
        }),
        toHandoffEvidence({
          stage: "batch_item_readback",
          method: "GET",
          path: `/rest/api/2/issue/${done.issue.key}`,
          httpStatus: readbackStatus,
        })
      );
    } catch (error) {
      if (error instanceof JiraAmbiguousOutcome) {
        unknown += 1;
        const entry = {
          index: item.index,
          summary: item.summary,
          state: "UnknownOutcome",
          ...(typeof error.issueKey === "string" ? { key: error.issueKey } : {}),
          ...(error.verification ? { verification: error.verification } : {}),
        };
        results.push(entry);
        await persistUnknown(item.index, {
          index: item.index,
          summary: item.summary,
          ...(typeof error.issueKey === "string" ? { key: error.issueKey } : {}),
          ...(error.verification ? { verification: error.verification } : {}),
        });
        handoff.push(
          toHandoffEvidence({
            stage: "batch_item_submission",
            method: "POST",
            path: "/rest/api/2/issue",
            httpStatus: "unknown",
            note: "outcome unknown; no automatic retry",
          })
        );
        continue;
      }
      if (error instanceof JiraProtocolError && error.stage === "batch_journal") {
        error.details = {
          ...(error.details ?? {}),
          partialResults: [...results],
          partialHandoff: [...handoff],
          ...(journalPath ? { journalPath, batchId } : {}),
        };
        throw error;
      }
      if (error instanceof JiraProtocolError && (error.status === 401 || error.status === 403)) {
        // Terminal policy denial: the submission was definitively rejected,
        // so the in-flight marker for this index is proven stale and cleared
        // before aborting with partial results preserved.
        await clearInflight(item.index);
        error.details = {
          ...(error.details ?? {}),
          partialResults: [...results],
          partialHandoff: [...handoff],
          ...(journalPath ? { journalPath, batchId } : {}),
        };
        throw error;
      }
      if (error instanceof JiraProtocolError && isProvenPreDispatchFailure(error)) {
        await clearInflight(item.index);
        failed += 1;
        results.push({
          index: item.index,
          summary: item.summary,
          state: "Failed",
          error: error instanceof Error ? error.message : "unknown error",
        });
        continue;
      }
      // Uncertain failure: a mutation may have started, so keep a blocking
      // marker and require explicit verified resolution. Never misclassify
      // as a safe Failed retry.
      unknown += 1;
      const uncertain = {
        index: item.index,
        summary: item.summary,
        state: "UnknownOutcome",
      };
      results.push({
        ...uncertain,
        error: error instanceof Error ? error.message : "unknown error",
      });
      await persistUnknown(item.index, uncertain);
      handoff.push(
        toHandoffEvidence({
          stage: "batch_item_submission",
          method: "POST",
          path: "/rest/api/2/issue",
          httpStatus: "unknown",
          note: "outcome unknown; no automatic retry",
        })
      );
    }
  }
  for (const item of planned.items.filter((entry) => entry.match === "exists")) {
    const entry = {
      index: item.index,
      summary: item.summary,
      state: "AlreadyExists",
      key: item.existingKeys[0],
      link: `${CONTRACT.origin}/browse/${item.existingKeys[0]}`,
    };
    results.push(entry);
    if (journalPath) await persistSuccess(item.index, entry);
  }
  // Count journaled successes that were skipped above (already in results).
  results.sort((left, right) => left.index - right.index);
  const receipt = {
    operation: planned.operation,
    confirmation: planned.confirmation,
    model: planned.model,
    existing: planned.existing,
    items: planned.items,
    results,
    writes: runWrites,
    handoff,
    ...(journalView
      ? {
          journal: {
            ...journalView,
            resumed: journal
              ? Object.keys(journal.results ?? {})
                  .map(Number)
                  .sort((a, b) => a - b)
              : journalView.resumed,
            blockedUnknown: journal ? journalBlockedIndexes(journal) : [],
          },
        }
      : {}),
  };
  // Journaled UnknownOutcome or leftover in-flight entries from a previous
  // run that were resolved to an operator-verified key are already in
  // results; count any remaining in-journal blocking entries (written this
  // run) via the local counter plus the journal buckets.
  const journaledUnknownCount = journal ? journalBlockedIndexes(journal).length : 0;
  if (unknown > 0 || journaledUnknownCount > 0) return { state: "UnknownOutcome", ...receipt };
  if (failed > 0) return { state: "Failed", ...receipt };
  return { state: "Completed", ...receipt };
}
