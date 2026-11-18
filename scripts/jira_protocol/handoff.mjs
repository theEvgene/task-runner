import { JiraProtocolError } from "./protocol.mjs";

export const HANDOFF_SCHEMA = "jira-handoff-evidence/1";

export const STEP_CATEGORIES = Object.freeze([
  "observed",
  "inferred",
  "assumed",
]);

const METHODS = new Set(["GET", "POST", "PUT"]);

const SECRET_KEY = /^(?:atl_?token|formtoken|authorization|cookie|set-cookie)$/i;

function nonEmptyString(value, name) {
  const text = String(value ?? "").trim();
  if (!text) throw new JiraProtocolError(`${name} is required`);
  return text;
}

function stringList(value, name) {
  if (!Array.isArray(value))
    throw new JiraProtocolError(`${name} must be a list of strings`);
  for (const entry of value) {
    if (typeof entry !== "string" || !entry.trim())
      throw new JiraProtocolError(`${name} must be a list of strings`);
  }
  return [...value];
}

// A ledger entry for one protocol step of the handoff scenario. Category is
// one of: observed (seen in a sanitized capture), inferred (derived from
// client code or a REST contract), assumed (taken on faith until a live
// readback proves it). An unverified step must stay unknown and list its
// gaps; it must never be recorded as verified.
export function handoffStep({
  step,
  stage,
  category,
  source,
  verification = null,
  gaps = [],
}) {
  const name = nonEmptyString(step, "step");
  const token = nonEmptyString(stage, "stage");
  if (!STEP_CATEGORIES.includes(category))
    throw new JiraProtocolError(
      `category must be one of: ${STEP_CATEGORIES.join(", ")}`
    );
  const origin = nonEmptyString(source, "source");
  const checked = stringList(gaps, "gaps");
  let proven = null;
  if (verification !== null && verification !== undefined) {
    proven = nonEmptyString(verification, "verification");
  }
  if (proven === null && checked.length === 0)
    throw new JiraProtocolError(
      "an unverified step must list its evidence gaps"
    );
  return Object.freeze({
    step: name,
    stage: token,
    category,
    source: origin,
    verification: proven,
    gaps: Object.freeze(checked),
  });
}

function sanitizePath(value) {
  const raw = nonEmptyString(value, "path");
  const stripped = raw.split("#", 1)[0].split("?", 1)[0];
  if (!stripped.startsWith("/"))
    throw new JiraProtocolError("handoff path must stay on the Jira origin");
  if (/[\s\u0000-\u001f\u007f]/.test(stripped))
    throw new JiraProtocolError("handoff path contains unsafe characters");
  return stripped;
}

// Sanitized transport evidence for one authorized step: fixed stage token,
// HTTP method, query-free path, and the observed HTTP status. Anything not
// actually observed is recorded as "unknown", never invented. By
// construction this carries no credential material, query string, header,
// or request/response body.
export function toHandoffEvidence({
  stage,
  method,
  path,
  httpStatus = "unknown",
  note,
} = {}) {
  const token = nonEmptyString(stage, "stage");
  if (!/^[a-z][a-z0-9_]*$/.test(token))
    throw new JiraProtocolError("handoff stage must be a fixed stage token");
  const verb = String(method ?? "").trim().toUpperCase();
  if (!METHODS.has(verb))
    throw new JiraProtocolError("handoff method must be GET, POST, or PUT");
  const clean = sanitizePath(path);
  let status = "unknown";
  if (httpStatus !== null && httpStatus !== undefined && httpStatus !== "unknown") {
    const parsed = Number(httpStatus);
    if (!Number.isSafeInteger(parsed) || parsed < 100 || parsed > 599)
      throw new JiraProtocolError("handoff httpStatus is invalid");
    status = parsed;
  }
  const evidence = { schema: HANDOFF_SCHEMA, stage: token, method: verb, path: clean, httpStatus: status };
  if (note !== undefined) evidence.note = nonEmptyString(note, "note");
  return Object.freeze(evidence);
}

// Fail-closed sweep for sanitized receipts: secret-bearing keys must either
// be absent or carry the "[REDACTED]" marker. Handoff evidence objects must
// additionally keep their exact key set so no body, query, header, or
// credential field can ride along.
export function assertHandoffClean(value, label = "handoff evidence") {
  const failures = [];
  const walk = (node, trail) => {
    if (Array.isArray(node)) {
      node.forEach((entry, index) => walk(entry, `${trail}[${index}]`));
      return;
    }
    if (node && typeof node === "object") {
      if (node.schema === HANDOFF_SCHEMA) {
        const allowed = new Set([
          "schema",
          "stage",
          "method",
          "path",
          "httpStatus",
          "note",
        ]);
        for (const key of Object.keys(node)) {
          if (!allowed.has(key))
            failures.push(`${trail || "<root>"}: unexpected key ${key}`);
        }
      }
      for (const [key, entry] of Object.entries(node)) {
        if (SECRET_KEY.test(key) && entry !== "[REDACTED]")
          failures.push(`${trail || "<root>"}: secret key ${key} is not redacted`);
        walk(entry, trail ? `${trail}.${key}` : key);
      }
    }
  };
  walk(value, "");
  if (failures.length)
    throw new JiraProtocolError(
      `${label} carries secret or unexpected material: ${failures.join("; ")}`
    );
  return true;
}
