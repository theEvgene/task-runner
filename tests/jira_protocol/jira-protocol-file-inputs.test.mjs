import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { resolveFileBackedText } from "../../scripts/jira-protocol.mjs";

const root = fileURLToPath(new URL("../../", import.meta.url));
const cli = join(root, "scripts", "jira-protocol.mjs");

test("file-backed text preserves quotes, Unicode, and multiline exactly", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "jira-file-input-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const body = 'He said "hi" & <bye>\nline2 éß 中文\nline3';
  const file = join(dir, "comment.md");
  await writeFile(file, body, "utf8");
  const resolved = await resolveFileBackedText(
    { "comment-file": file },
    "comment",
    "comment-file"
  );
  assert.equal(resolved, body);
  assert.equal(await readFile(file, "utf8"), body);
});

test("inline and file forms are mutually exclusive", async () => {
  await assert.rejects(
    resolveFileBackedText(
      { comment: "x", "comment-file": "y" },
      "comment",
      "comment-file"
    ),
    /mutually exclusive/
  );
});

test("missing file refuses without credentials", async () => {
  await assert.rejects(
    resolveFileBackedText(
      { "comment-file": join(tmpdir(), "jira-protocol-does-not-exist.md") },
      "comment",
      "comment-file"
    ),
    /unreadable/
  );
});

test("CLI refuses --comment with --comment-file together", () => {
  const result = spawnSync(
    process.execPath,
    [
      cli,
      "flag",
      "--issue",
      "LEL-1",
      "--action",
      "add",
      "--comment",
      "x",
      "--comment-file",
      "y",
    ],
    { encoding: "utf8" }
  );
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}${result.stderr}`, /mutually exclusive/);
});

test("CLI refuses --summary with --summary-file together", () => {
  const result = spawnSync(
    process.execPath,
    [cli, "create-epic", "--summary", "x", "--summary-file", "y"],
    { encoding: "utf8" }
  );
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}${result.stderr}`, /mutually exclusive/);
});
