#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  JiraTransport,
  redactSecrets,
} from "./jira_protocol/protocol.mjs";
import {
  CONTRACT,
  assignIssue,
  flagIssue,
  createEpic,
  createSubtask,
  getIssue,
  getIssueTransitions,
  transitionIssue,
} from "./jira_protocol/operations.mjs";
import { transitionToStatus } from "./jira_protocol/route.mjs";
import {
  createSubtaskBatch,
  loadBatchFile,
  parseBatchFile,
} from "./jira_protocol/batch.mjs";
import { extractIssue } from "./jira_protocol/extract.mjs";
import { findSiblingIssues } from "./jira_protocol/siblings.mjs";
import {
  formatForQueue,
  readCompactFields,
} from "./jira_protocol/read-fields.mjs";

function usage(message) {
  throw new JiraProtocolError(
    message ??
      "usage: jira-protocol <extract|read-fields|siblings|detail-view|transitions|transition|transition-to-status|assign|flag|create-epic|create-subtask|create-subtasks> [options]"
  );
}

function parse(argv) {
  const [command, ...tokens] = argv;
  if (!command) usage();
  const repeatable = new Set(["field", "allow-duplicate", "resolve-unknown"]);
  const values = { field: [], "allow-duplicate": [], "resolve-unknown": [] };
  const boolean = new Set(["--help"]);
  for (let index = 0; index < tokens.length; index += 1) {
    const name = tokens[index];
    if (!name.startsWith("--")) usage(`unexpected argument ${name}`);
    if (boolean.has(name)) {
      values[name.slice(2)] = true;
      continue;
    }
    const value = tokens[index + 1];
    if (value === undefined || value.startsWith("--"))
      usage(`missing value for ${name}`);
    index += 1;
    const key = name.slice(2);
    if (repeatable.has(key)) values[key].push(value);
    else if (Object.hasOwn(values, key)) usage(`duplicate option ${name}`);
    else values[key] = value;
  }
  return { command, values };
}

export async function resolveFileBackedText(values, inlineName, fileName) {
  const inline = values[inlineName];
  const file = values[fileName];
  if (inline !== undefined && file !== undefined)
    throw new JiraProtocolError(
      `--${inlineName} and --${fileName} are mutually exclusive`
    );
  if (file === undefined) return inline;
  try {
    return await readFile(file, "utf8");
  } catch (error) {
    throw new JiraProtocolError(`--${fileName} is unreadable`, {
      details: { path: file },
      ...(error?.code ? { details: { path: file, code: error.code } } : {}),
    });
  }
}

function only(values, allowed) {
  const accepted = new Set([...allowed, "help"]);
  for (const key of Object.keys(values)) {
    if (
      (key === "field" ||
        key === "allow-duplicate" ||
        key === "resolve-unknown") &&
      values[key].length === 0
    )
      continue;
    if (!accepted.has(key) && values[key] !== undefined)
      usage(`unknown option --${key}`);
  }
}

function fields(entries) {
  const result = {};
  for (const entry of entries) {
    const separator = entry.indexOf("=");
    if (separator <= 0) usage("--field must use fieldId=value");
    const key = entry.slice(0, separator);
    if (Object.hasOwn(result, key)) usage(`duplicate field ${key}`);
    result[key] = entry.slice(separator + 1);
  }
  return result;
}

export function serializeReceipt(value) {
  return `${JSON.stringify(redactSecrets(value), null, 2)}\n`;
}

function emit(value) {
  process.stdout.write(serializeReceipt(value));
}

async function main() {
  const { command, values } = parse(process.argv.slice(2));
  if (values.help) usage();
  const transport = () =>
    new JiraTransport({
      origin: process.env.JIRA_BASE_URL ?? CONTRACT.origin,
      cookie: process.env.JIRA_SESSION_COOKIE,
      atlToken: process.env.JIRA_ATL_TOKEN,
      allowTestOrigin: process.env.JIRA_PROTOCOL_TEST_MODE === "1",
    });
  if (command === "extract") {
    only(values, ["issue", "page-size", "max-pages"]);
    const result = await extractIssue(transport(), values.issue, {
      pageSize: values["page-size"],
      maxPages: values["max-pages"],
    });
    emit(result);
    return result.completeness.comprehensive ? 0 : 6;
  }
  if (command === "siblings") {
    only(values, ["issue", "assignee"]);
    const result = await findSiblingIssues(transport(), {
      issueKey: values.issue,
      assignee: values.assignee,
    });
    emit(result);
    return result.completeness.comprehensive ? 0 : 6;
  }
  if (command === "read-fields") {
    only(values, ["issue", "fields", "max-bytes"]);
    const result = await readCompactFields(transport(), {
      issueKey: values.issue,
      fields: values.fields,
    });
    if (values["max-bytes"] !== undefined) {
      const maxBytes = Number(values["max-bytes"]);
      if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0)
        throw new JiraProtocolError("max-bytes must be a positive integer");
      const { json, truncated } = formatForQueue(result, { maxBytes });
      // Write compact JSON so the measured UTF-8 bytes match the actual
      // stdout payload; pretty-printing would inflate past the budget.
      const output = `${json}\n`;
      if (Buffer.byteLength(output, "utf8") > maxBytes)
        throw new JiraProtocolError(
          "queue output still exceeds max-bytes budget; refusing to emit oversized output"
        );
      if (truncated) {
        process.stderr.write(
          `jira-protocol read-fields: output truncated at ${maxBytes} bytes; receipt is incomplete\n`
        );
        process.stdout.write(output);
        return 6;
      }
      process.stdout.write(output);
      return result.completeness.comprehensive ? 0 : 6;
    }
    emit(result);
    return result.completeness.comprehensive ? 0 : 6;
  }
  if (command === "detail-view") {
    only(values, ["issue"]);
    emit({
      schema: "jira-protocol-result/1",
      operation: "detail-view",
      dataContract: {
        kind: "greenhopper-detail-view",
        comprehensive: false,
        provenance:
          "/rest/greenhopper/1.0/xboard/issue/details.json?loadSubtasks=true",
        knownGaps: [
          "not the Jira REST issue resource",
          "no evidenced complete comments/history/attachments pagination contract",
        ],
      },
      detailView: await getIssue(transport(), values.issue),
    });
    return 0;
  }
  if (command === "transitions") {
    only(values, ["issue"]);
    const result = await getIssueTransitions(transport(), values.issue);
    emit({
      schema: "jira-protocol-result/1",
      operation: "transitions",
      ...result,
    });
    return 0;
  }
  if (command === "transition") {
    only(values, ["issue", "transition-id", "target-column", "confirm"]);
    const result = await transitionIssue(transport(), {
      issueKey: values.issue,
      transitionId: values["transition-id"],
      targetColumn: values["target-column"],
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    return result.state === "Completed" ? 0 : 3;
  }
  if (command === "transition-to-status") {
    only(values, ["issue", "status", "confirm"]);
    const result = await transitionToStatus(transport(), {
      issueKey: values.issue,
      status: values.status,
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    return result.state === "Completed" ? 0 : 3;
  }
  if (command === "assign") {
    only(values, ["issue", "assignee", "confirm"]);
    const result = await assignIssue(transport(), {
      issueKey: values.issue,
      assignee: values.assignee,
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    return result.state === "Completed" ? 0 : 3;
  }
  if (command === "flag") {
    only(values, ["issue", "action", "comment", "comment-file", "confirm"]);
    const comment = await resolveFileBackedText(
      values,
      "comment",
      "comment-file"
    );
    const result = await flagIssue(transport(), {
      issueKey: values.issue,
      action: values.action,
      comment,
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    return result.state === "Completed" ? 0 : 3;
  }
  if (command === "create-epic") {
    only(values, [
      "summary",
      "summary-file",
      "description",
      "description-file",
      "confirm",
      "field",
    ]);
    const summary = await resolveFileBackedText(
      values,
      "summary",
      "summary-file"
    );
    const description = await resolveFileBackedText(
      values,
      "description",
      "description-file"
    );
    const result = await createEpic(transport(), {
      summary,
      description,
      fields: fields(values.field),
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    if (result.state === "Completed") return 0;
    return result.state === "MissingRequiredFields" ? 4 : 3;
  }
  if (command === "create-subtask") {
    only(values, [
      "parent",
      "summary",
      "summary-file",
      "description",
      "description-file",
      "issue-type-id",
      "confirm",
      "field",
    ]);
    const summary = await resolveFileBackedText(
      values,
      "summary",
      "summary-file"
    );
    const description = await resolveFileBackedText(
      values,
      "description",
      "description-file"
    );
    const result = await createSubtask(transport(), {
      parent: values.parent,
      summary,
      description,
      issueTypeId: values["issue-type-id"],
      fields: fields(values.field),
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    if (result.state === "Completed") return 0;
    return result.state === "MissingRequiredFields" ? 4 : 3;
  }
  if (command === "create-subtasks") {
    only(values, [
      "parent",
      "file",
      "issue-type-id",
      "allow-duplicate",
      "confirm",
      "journal",
      "resolve-unknown",
    ]);
    if (values.file === undefined)
      throw new JiraProtocolError("--file is required");
    // Crash-safe writes require a durable journal: a confirmed batch must
    // persist an in-flight marker before each POST, so a stale child
    // listing after a crash cannot cause a blind duplicate. Plan-only runs
    // (no --confirm) stay journal-free.
    if (values.confirm !== undefined && values.journal === undefined)
      throw new JiraProtocolError(
        "--journal is required for a confirmed create-subtasks write; re-run the plan with --journal <path> and the same inputs"
      );
    const items = parseBatchFile(await loadBatchFile(values.file));
    const result = await createSubtaskBatch(transport(), {
      parent: values.parent,
      items,
      issueTypeId: values["issue-type-id"],
      allowDuplicates: values["allow-duplicate"],
      confirm: values.confirm,
      journalPath: values.journal,
      resolveUnknown: values["resolve-unknown"],
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    if (result.state === "Completed") return 0;
    if (result.state === "MissingRequiredFields") return 4;
    if (result.state === "UnknownOutcome") return 5;
    if (result.state === "Failed") return 2;
    return 3;
  }
  usage(`unknown command ${command}`);
}

export function errorReceipt(error) {
  const ambiguous = error instanceof JiraAmbiguousOutcome;
  const expected = ambiguous || error instanceof JiraProtocolError;
  return {
    schema: "jira-protocol-error/1",
    state: ambiguous ? "UnknownOutcome" : "Failed",
    error: expected ? error.message : "unexpected internal error",
    ...(ambiguous && error.issueKey ? { issueKey: error.issueKey } : {}),
    ...(ambiguous && error.verification
      ? { verification: error.verification }
      : {}),
    ...(error instanceof JiraProtocolError && error.status
      ? { httpStatus: error.status }
      : {}),
    ...(expected &&
    [
      "create_form_model",
      "create_submission",
      "subtask_meta",
      "subtask_parent",
      "subtask_submission",
      "batch_existing",
      "batch_journal",
    ].includes(error.stage)
      ? { stage: error.stage }
      : {}),
    ...(error instanceof JiraProtocolError && error.details
      ? { details: error.details }
      : {}),
  };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  try {
    process.exitCode = await main();
  } catch (error) {
    const ambiguous = error instanceof JiraAmbiguousOutcome;
    emit(errorReceipt(error));
    process.exitCode = ambiguous ? 5 : 2;
  }
}
