import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = new URL("../../", import.meta.url);
const skill = (name, file = "SKILL.md") =>
  new URL(`skills/${name}/${file}`, root);

test("compact skill binds the command, ledger, and queue route", async () => {
  const text = await readFile(skill("cli-read-selected-fields"), "utf8");
  assert.match(text, /^---\r?$/m);
  assert.match(text, /name: cli-read-selected-fields/);
  assert.match(
    text,
    /jira-protocol\.mjs read-fields --issue LEL-1234 --fields summary,status,assignee/
  );
  assert.match(text, /--max-bytes/);
  assert.match(text, /completeness\.comprehensive/);
  assert.match(text, /visibilityLimits/);
  assert.match(text, /field_unobserved_permission_or_configuration/);
  assert.match(text, /customfield_10641/);
  assert.match(text, /commentsFetched/);
  assert.match(text, /parentFetched/);
  assert.match(text, /[Nn]ever merge these short entries/);
  assert.match(text, /HTTP 401\/403 is terminal/);
  assert.match(text, /scripts\/jira-protocol\.mjs/);
  assert.match(text, /references\/jira-protocol\.md/);
  assert.match(text, /references\/jira-protocol-read-fields\.md/);
  assert.match(text, /relative to this `SKILL\.md`/);
  assert.match(text, /lenovo-hosted-scripts\.md/);
});

test("compact receipt example stays out of the skill", async () => {
  const rootPath = fileURLToPath(root);
  assert.ok(rootPath.length > 0);
  const text = await readFile(skill("cli-read-selected-fields"), "utf8");
  assert.doesNotMatch(text, /JIRA_SESSION_COOKIE='[^']{8}/);
});
