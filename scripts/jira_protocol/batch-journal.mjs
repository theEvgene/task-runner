import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { JiraProtocolError } from "./protocol.mjs";

export const BATCH_JOURNAL_SCHEMA = "jira-subtasks-journal/1";

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
  if (
    !parsed ||
    typeof parsed !== "object" ||
    parsed.schema !== BATCH_JOURNAL_SCHEMA
  )
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
    if (error instanceof JiraProtocolError && error.stage === "batch_journal")
      throw error;
    throw Object.assign(
      new JiraProtocolError("--journal write failed", {
        details: { path, ...(error?.code ? { code: error.code } : {}) },
      }),
      { stage: "batch_journal" }
    );
  }
}

export function journalResultFor(journal, index) {
  const bucket = journal?.results;
  if (!bucket || typeof bucket !== "object") return undefined;
  return bucket[String(index)];
}

export function journalUnknownFor(journal, index) {
  const bucket = journal?.unknown;
  if (!bucket || typeof bucket !== "object") return undefined;
  return bucket[String(index)];
}

export function journalInflightFor(journal, index) {
  const bucket = journal?.inflight;
  if (!bucket || typeof bucket !== "object") return undefined;
  return bucket[String(index)];
}

export function journalBlockedIndexes(journal) {
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
