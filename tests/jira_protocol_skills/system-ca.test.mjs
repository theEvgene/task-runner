import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../../", import.meta.url);
const skill = (name) => new URL(`skills/${name}/SKILL.md`, root);
const doc = (name) => new URL(`docs/${name}`, root);
const script = (name) => new URL(`scripts/${name}`, root);

const names = [
  "cli-create-lel-service-backlog-issue",
  "cli-assign-jira-issue",
  "cli-transition-jira-issue",
  "cli-extract-jira-content",
  "cli-flag-jira-issue",
];

const INVOKE = /jira-protocol\.mjs/;
const FORBIDDEN = [
  "NODE_TLS_REJECT_UNAUTHORIZED=0",
  "$env:NODE_TLS_REJECT_UNAUTHORIZED",
  "rejectUnauthorized:false",
  "rejectUnauthorized: false",
  "rejectUnauthorized=false",
  "strictSSL=false",
  "strictSSL: false",
  "--insecure",
  "NODE_NO_WARNINGS",
];

test("every Jira skill launches the protocol CLI with NODE_USE_SYSTEM_CA=1", async () => {
  for (const name of names) {
    const text = await readFile(skill(name), "utf8");
    assert.match(text, /NODE_USE_SYSTEM_CA=1/, `${name} must name the env var`);
    assert.match(
      text,
      /TLS verification stays enabled/,
      `${name} must keep TLS verification`
    );
    const blocks = [
      ...text.matchAll(/```(?:text|powershell)\r?\n([\s\S]*?)```/g),
    ].map((match) => match[1]);
    const invocations = blocks.filter((block) => INVOKE.test(block));
    assert.ok(
      invocations.length > 0,
      `${name} must show a jira-protocol.mjs invocation`
    );
    for (const block of invocations)
      assert.match(
        block,
        /NODE_USE_SYSTEM_CA/,
        `${name} code block must carry NODE_USE_SYSTEM_CA`
      );
  }
});

test("protocol docs keep system CA without disabling TLS verification", async () => {
  const text = await readFile(doc("jira-protocol.md"), "utf8");
  assert.match(text, /NODE_USE_SYSTEM_CA=1/);
  assert.match(text, /TLS verification stays enabled/);
  for (const pattern of FORBIDDEN)
    assert.doesNotMatch(
      text,
      new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
      `docs must not contain ${pattern}`
    );
});

test("skills and runtime add no TLS bypass or error-handling shim", async () => {
  const targets = [
    ...names.map((name) => skill(name)),
    doc("jira-protocol.md"),
    doc("jira-protocol-subtasks.md"),
    script("jira-protocol.mjs"),
    script("jira_protocol/protocol.mjs"),
    script("jira_protocol/operations.mjs"),
    script("jira_protocol/operations-shared.mjs"),
    script("jira_protocol/operations-transition.mjs"),
    script("jira_protocol/operations-assign.mjs"),
    script("jira_protocol/operations-epic.mjs"),
    script("jira_protocol/operations-subtask.mjs"),
    script("jira_protocol/operations-flag.mjs"),
    script("jira_protocol/extract.mjs"),
    script("jira_protocol/html-form.mjs"),
  ];
  for (const target of targets) {
    const text = await readFile(target, "utf8");
    for (const pattern of FORBIDDEN)
      assert.doesNotMatch(
        text,
        new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
        `${target} must not contain ${pattern}`
      );
  }
});
