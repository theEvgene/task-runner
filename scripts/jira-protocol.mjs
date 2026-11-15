#!/usr/bin/env node
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
  createEpic,
  getIssue,
  getIssueTransitions,
  transitionIssue,
} from "./jira_protocol/operations.mjs";
import { extractIssue } from "./jira_protocol/extract.mjs";

function usage(message) {
  throw new JiraProtocolError(
    message ??
      "usage: jira-protocol <extract|detail-view|transitions|transition|assign|create-epic> [options]"
  );
}

function parse(argv) {
  const [command, ...tokens] = argv;
  if (!command) usage();
  const values = { field: [] };
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
    if (key === "field") values.field.push(value);
    else if (Object.hasOwn(values, key)) usage(`duplicate option ${name}`);
    else values[key] = value;
  }
  return { command, values };
}

function only(values, allowed) {
  const accepted = new Set([...allowed, "help"]);
  for (const key of Object.keys(values)) {
    if (key === "field" && values.field.length === 0) continue;
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
  if (command === "create-epic") {
    only(values, ["summary", "description", "confirm", "field"]);
    const result = await createEpic(transport(), {
      summary: values.summary,
      description: values.description,
      fields: fields(values.field),
      confirm: values.confirm,
    });
    emit({ schema: "jira-protocol-result/1", ...result });
    if (result.state === "Completed") return 0;
    return result.state === "MissingRequiredFields" ? 4 : 3;
  }
  usage(`unknown command ${command}`);
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  try {
    process.exitCode = await main();
  } catch (error) {
    const ambiguous = error instanceof JiraAmbiguousOutcome;
    const expected = ambiguous || error instanceof JiraProtocolError;
    emit({
      schema: "jira-protocol-error/1",
      state: ambiguous ? "UnknownOutcome" : "Failed",
      error: expected ? error.message : "unexpected internal error",
      ...(error instanceof JiraProtocolError && error.status
        ? { httpStatus: error.status }
        : {}),
      ...(error instanceof JiraProtocolError && error.details
        ? { details: error.details }
        : {}),
    });
    process.exitCode = ambiguous ? 5 : 2;
  }
}
