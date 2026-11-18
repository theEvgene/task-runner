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

test("CLI refuses --description with --description-file together", () => {
  const result = spawnSync(
    process.execPath,
    [cli, "create-epic", "--description", "x", "--description-file", "y"],
    { encoding: "utf8" }
  );
  assert.notEqual(result.status, 0);
  assert.match(`${result.stdout}${result.stderr}`, /mutually exclusive/);
});

test("long multiline Russian summary and description survive UTF-8 files exactly", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "jira-epic-ru-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const summary =
    "Длинный русский заголовок эпика «Устойчивость сервиса»\n" +
    'вторая строка с кавычками "тест" и символами <>& éß 中文 🙂';
  const description =
    "Описание эпика:\n" +
    "- строка 1: проверка «ёлочки» и „лапок“\n" +
    "- строка 2: символы éß 中文 emoji 🙂\n" +
    "\n" +
    "{code}node scripts/jira-protocol.mjs create-epic --summary-file s.txt --description-file d.txt{code}\n" +
    "Конец описания.";
  const summaryFile = join(dir, "summary.txt");
  const descriptionFile = join(dir, "description.md");
  await writeFile(summaryFile, summary, "utf8");
  await writeFile(descriptionFile, description, "utf8");
  const resolvedSummary = await resolveFileBackedText(
    { "summary-file": summaryFile },
    "summary",
    "summary-file"
  );
  const resolvedDescription = await resolveFileBackedText(
    { "description-file": descriptionFile },
    "description",
    "description-file"
  );
  assert.equal(resolvedSummary, summary);
  assert.equal(resolvedDescription, description);
  assert.equal(await readFile(summaryFile, "utf8"), summary);
  assert.equal(await readFile(descriptionFile, "utf8"), description);
});
