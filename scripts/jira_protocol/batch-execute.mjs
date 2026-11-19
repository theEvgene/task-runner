import { JiraAmbiguousOutcome, JiraProtocolError } from "./protocol.mjs";
import { CONTRACT, createSubtask } from "./operations.mjs";
import { toHandoffEvidence } from "./handoff.mjs";
import {
  batchIdentity,
  normalizeSummary,
  parseResolveUnknown,
} from "./batch-parse.mjs";
import {
  BATCH_JOURNAL_SCHEMA,
  journalBlockedIndexes,
  journalInflightFor,
  journalResultFor,
  journalUnknownFor,
  loadBatchJournal,
  saveBatchJournal,
} from "./batch-journal.mjs";
import { blockedItems, planSubtaskBatch } from "./batch-plan.mjs";

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
  const batchId = batchIdentity({
    parent,
    items,
    issueTypeId,
    allowDuplicates,
  });
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
      if (!journal.results || typeof journal.results !== "object")
        journal.results = {};
      if (!journal.unknown || typeof journal.unknown !== "object")
        journal.unknown = {};
      if (!journal.inflight || typeof journal.inflight !== "object")
        journal.inflight = {};
      // Apply explicit operator resolutions before any write. A resolution
      // applies to either a journaled UnknownOutcome or a leftover in-flight
      // marker from an interrupted run; both need operator verification.
      let mutated = false;
      for (const [index, decision] of resolvedDecisions) {
        const pending =
          journalUnknownFor(journal, index) ??
          journalInflightFor(journal, index);
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
  if (
    journal &&
    journal.resolvedIssueTypeId !== undefined &&
    journal.resolvedIssueTypeId !== null
  ) {
    if (
      String(journal.resolvedIssueTypeId) !==
      String(planned.operation?.issueTypeId ?? "")
    ) {
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
    if (!journal.results || typeof journal.results !== "object")
      journal.results = {};
    if (!journal.unknown || typeof journal.unknown !== "object")
      journal.unknown = {};
    if (!journal.inflight || typeof journal.inflight !== "object")
      journal.inflight = {};
    journal.updatedAt = new Date().toISOString();
    await saveBatchJournal(journalPath, journal);
  } else if (
    journal &&
    (!journal.inflight || typeof journal.inflight !== "object")
  ) {
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
    if (!journal.inflight || typeof journal.inflight !== "object")
      journal.inflight = {};
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
    const journaled = journal
      ? journalResultFor(journal, item.index)
      : undefined;
    if (
      journaled &&
      (journaled.state === "Created" || journaled.state === "AlreadyExists")
    ) {
      results.push(journaled);
      continue;
    }
    const input = {
      parent: planned.operation.parentKey,
      summary: item.summary,
      ...(item.description !== undefined
        ? { description: item.description }
        : {}),
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
          done.handoff.find((e) => e.stage === "subtask_submission")
            ?.httpStatus) ??
        "unknown";
      const readbackStatus =
        (Array.isArray(done.handoff) &&
          done.handoff.find((e) => e.stage === "subtask_readback")
            ?.httpStatus) ??
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
          ...(typeof error.issueKey === "string"
            ? { key: error.issueKey }
            : {}),
          ...(error.verification ? { verification: error.verification } : {}),
        };
        results.push(entry);
        await persistUnknown(item.index, {
          index: item.index,
          summary: item.summary,
          ...(typeof error.issueKey === "string"
            ? { key: error.issueKey }
            : {}),
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
      if (
        error instanceof JiraProtocolError &&
        error.stage === "batch_journal"
      ) {
        error.details = {
          ...(error.details ?? {}),
          partialResults: [...results],
          partialHandoff: [...handoff],
          ...(journalPath ? { journalPath, batchId } : {}),
        };
        throw error;
      }
      if (
        error instanceof JiraProtocolError &&
        (error.status === 401 || error.status === 403)
      ) {
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
      if (
        error instanceof JiraProtocolError &&
        isProvenPreDispatchFailure(error)
      ) {
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
  for (const item of planned.items.filter(
    (entry) => entry.match === "exists"
  )) {
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
  const journaledUnknownCount = journal
    ? journalBlockedIndexes(journal).length
    : 0;
  if (unknown > 0 || journaledUnknownCount > 0)
    return { state: "UnknownOutcome", ...receipt };
  if (failed > 0) return { state: "Failed", ...receipt };
  return { state: "Completed", ...receipt };
}
