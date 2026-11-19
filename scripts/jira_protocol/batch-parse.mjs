import { readFile } from "node:fs/promises";
import { JiraProtocolError, confirmationDigest } from "./protocol.mjs";

export const BATCH_MAX_ITEMS = 50;

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
    throw new JiraProtocolError(
      `batch item ${index}: fields must be an object`
    );
  const coerced = {};
  for (const [key, value] of Object.entries(fields)) {
    if (
      typeof value !== "string" &&
      typeof value !== "number" &&
      typeof value !== "boolean"
    )
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
    throw new JiraProtocolError(
      "batch file must hold a JSON array of subtasks"
    );
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
  const parentKey = String(parent ?? "")
    .trim()
    .toUpperCase();
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
    ...(item.description !== undefined
      ? { description: item.description }
      : {}),
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
