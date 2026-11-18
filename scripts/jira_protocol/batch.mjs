// Compatibility facade for the batch subtask protocol.
//
// The implementation lives in cohesive modules:
// - batch-parse.mjs (file format, identity, resolve-unknown parsing),
// - batch-journal.mjs (durable journal IO),
// - batch-plan.mjs (read-only matching plan),
// - batch-execute.mjs (confirmed executor).
// This file re-exports the public API so existing imports keep working.
export {
  BATCH_MAX_ITEMS,
  batchIdentity,
  loadBatchFile,
  normalizeSummary,
  parseBatchFile,
  parseResolveUnknown,
} from "./batch-parse.mjs";
export {
  BATCH_JOURNAL_SCHEMA,
  journalBlockedIndexes,
  journalInflightFor,
  journalResultFor,
  journalUnknownFor,
  loadBatchJournal,
  saveBatchJournal,
} from "./batch-journal.mjs";
export { blockedItems, planSubtaskBatch } from "./batch-plan.mjs";
export { createSubtaskBatch } from "./batch-execute.mjs";
