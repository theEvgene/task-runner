import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import test from "node:test";
import {
  createSubtask,
  getSubtaskCreateMeta,
} from "../../scripts/jira_protocol/operations.mjs";
import { JiraAmbiguousOutcome } from "../../scripts/jira_protocol/protocol.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const cli = join(here, "..", "..", "scripts", "jira-protocol.mjs");

function createmeta(extraSubtaskFields = {}) {
  return {
    projects: [
      {
        key: "LEL",
        id: "10848",
        issuetypes: [
          { id: "10100", name: "Task", subtask: false, fields: {} },
          {
            id: "10101",
            name: "Sub-task",
            subtask: true,
            fields: {
              summary: { name: "Summary", required: true },
              parent: { name: "Parent", required: true },
              project: { name: "Project", required: false },
              issuetype: { name: "Issue Type", required: false },
              description: { name: "Description", required: false },
              customfield_10481: {
                name: "Team",
                required: true,
                allowedValues: [
                  { id: "SERVICE", value: "SERVICE", name: "SERVICE" },
                ],
              },
              ...extraSubtaskFields,
            },
          },
        ],
      },
    ],
  };
}

function parentState(overrides = {}) {
  return {
    key: "LEL-42",
    id: "42",
    fields: {
      project: { id: "10848", key: "LEL" },
      issuetype: { id: "10100", name: "Task" },
      ...overrides,
    },
  };
}

class FakeTransport {
  constructor({ metaFields, parent = parentState(), meta } = {}) {
    this.calls = [];
    this.meta = meta ? structuredClone(meta) : createmeta(metaFields);
    this.parent = parent;
    this.created = null;
    this.createdState = null;
    this.readbackOverride = null;
  }

  async request(request) {
    this.calls.push(request);
    if (request.path === "/rest/api/2/issue/createmeta")
      return { payload: structuredClone(this.meta) };
    if (request.method === "POST" && request.path === "/rest/api/2/issue") {
      const fields = request.json?.fields ?? {};
      this.created = structuredClone(fields);
      const summary = typeof fields.summary === "string" ? fields.summary : fields.summary;
      const description =
        typeof fields.description === "string" ? fields.description : fields.description;
      const team = fields.customfield_10481;
      const createdTypeId =
        fields.issuetype && fields.issuetype.id !== undefined && fields.issuetype.id !== null
          ? String(fields.issuetype.id)
          : "10101";
      this.createdState = {
        key: "LEL-99",
        id: "99",
        fields: {
          project: { id: "10848", key: "LEL" },
          issuetype: { id: createdTypeId, name: "Sub-task" },
          parent: { key: "LEL-42", id: "42" },
          summary,
          description,
          customfield_10481: team ?? null,
        },
      };
      return { payload: { key: "LEL-99", id: "99" } };
    }
    if (request.path === "/rest/api/2/issue/LEL-42")
      return { payload: structuredClone(this.parent) };
    if (request.path === "/rest/api/2/issue/LEL-99") {
      if (this.readbackOverride) return { payload: this.readbackOverride(request) };
      return { payload: structuredClone(this.createdState) };
    }
    throw new Error(`unexpected request ${request.method ?? "GET"} ${request.path}`);
  }
}

test("subtask metadata resolves the single LEL subtask type", async () => {
  const meta = await getSubtaskCreateMeta(new FakeTransport());
  assert.equal(meta.subtaskType.id, "10101");
  assert.match(meta.subtaskType.name, /Sub/i);
});

test("subtask plan requires an explicit parent", async () => {
  await assert.rejects(
    createSubtask(new FakeTransport(), { summary: "x" }),
    /parent is required/
  );
  await assert.rejects(
    createSubtask(new FakeTransport(), { parent: "OTHER-1", summary: "x" }),
    /must be an LEL issue key/
  );
});

test("subtask reports live required fields before any write", async () => {
  const transport = new FakeTransport();
  const result = await createSubtask(transport, {
    parent: "LEL-42",
    summary: "Fix it",
  });
  assert.equal(result.state, "MissingRequiredFields");
  assert.deepEqual(result.model.missingFields, [{ id: "customfield_10481", label: "Team" }]);
  assert.equal(result.model.subtaskType.id, "10101");
  assert.equal(result.model.parent.key, "LEL-42");
  assert.equal(
    transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
    false
  );
});

test("subtask plan then confirmed write verifies parent linkage and fields", async () => {
  const transport = new FakeTransport();
  const input = {
    parent: "lel-42",
    summary: "Fix it",
    description: "Details",
    fields: { customfield_10481: "SERVICE" },
  };
  const preview = await createSubtask(transport, input);
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.parentKey, "LEL-42");
  assert.equal(preview.operation.issueTypeId, "10101");
  const result = await createSubtask(transport, { ...input, confirm: preview.confirmation });
  assert.equal(result.state, "Completed");
  assert.equal(result.issue.key, "LEL-99");
  assert.equal(result.readback.projectKey, "LEL");
  assert.equal(result.readback.issueTypeId, "10101");
  assert.equal(result.readback.parentKey, "LEL-42");
  assert.equal(result.readback.persistedFields.summary.matched, true);
  assert.equal(result.readback.persistedFields.description.matched, true);
  assert.equal(result.readback.persistedFields.customfield_10481.matched, true);
  assert.equal(transport.created.project.id, "10848");
  assert.equal(transport.created.parent.key, "LEL-42");
  assert.equal(transport.created.issuetype.id, "10101");
});

test("subtask confirmation changes when live metadata changes", async () => {
  const transport = new FakeTransport();
  const input = {
    parent: "LEL-42",
    summary: "Fix it",
    fields: { customfield_10481: "SERVICE" },
  };
  const preview = await createSubtask(transport, input);
  transport.meta.projects[0].issuetypes[1].fields.customfield_10999 = {
    name: "Extra",
    required: false,
  };
  const second = await createSubtask(transport, { ...input, confirm: preview.confirmation });
  assert.equal(second.state, "AwaitingConfirmation");
  assert.notEqual(second.confirmation, preview.confirmation);
  assert.equal(
    transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
    false
  );
});

test("subtask rejects fields absent from live metadata", async () => {
  const transport = new FakeTransport();
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Fix it",
      fields: { customfield_99999: "x" },
    }),
    /absent from the live create metadata/
  );
  assert.equal(
    transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
    false
  );
});

test("subtask rejects reserved system fields even when live metadata declares them", async () => {
  for (const reserved of ["parent", "project", "issuetype"]) {
    const transport = new FakeTransport();
    await assert.rejects(
      createSubtask(transport, {
        parent: "LEL-42",
        summary: "Fix it",
        fields: { [reserved]: "LEL-999", customfield_10481: "SERVICE" },
      }),
      /managed by the subtask CLI/
    );
    assert.equal(
      transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
      false,
      `reserved field ${reserved} must not reach submission`
    );
  }
});

test("subtask rejects a parent without an issue type id", async () => {
  for (const issuetype of [{}, { id: null }, { id: "" }, { id: "  " }]) {
    const transport = new FakeTransport({
      parent: parentState({ issuetype }),
    });
    await assert.rejects(
      createSubtask(transport, {
        parent: "LEL-42",
        summary: "Fix it",
        fields: { customfield_10481: "SERVICE" },
      }),
      /does not declare an issue type id/
    );
    assert.equal(
      transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
      false
    );
  }
});

test("subtask refuses a parent that is itself a subtask", async () => {
  const transport = new FakeTransport({
    parent: parentState({ issuetype: { id: "10101", name: "Sub-task" } }),
  });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Fix it",
      fields: { customfield_10481: "SERVICE" },
    }),
    /cannot have a child subtask/
  );
});

test("subtask refuses a parent outside LEL", async () => {
  const transport = new FakeTransport({
    parent: parentState({ project: { id: "99999", key: "OTHER" } }),
  });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Fix it",
      fields: { customfield_10481: "SERVICE" },
    }),
    /not in the LEL project/
  );
});

test("subtask readback mismatch is UnknownOutcome with the key", async () => {
  const transport = new FakeTransport();
  const input = {
    parent: "LEL-42",
    summary: "Fix it",
    fields: { customfield_10481: "SERVICE" },
  };
  const preview = await createSubtask(transport, input);
  transport.readbackOverride = () => ({
    key: "LEL-99",
    id: "99",
    fields: {
      project: { id: "10848", key: "LEL" },
      issuetype: { id: "10101", name: "Sub-task" },
      parent: { key: "LEL-42", id: "42" },
      summary: "Fix it",
      customfield_10481: { value: "OTHER" },
    },
  });
  await assert.rejects(
    createSubtask(transport, { ...input, confirm: preview.confirmation }),
    (error) => {
      assert.ok(error instanceof JiraAmbiguousOutcome);
      assert.equal(error.issueKey, "LEL-99");
      assert.equal(error.verification.fieldsPersisted, false);
      return true;
    }
  );
});

test("subtask wrong-parent readback is UnknownOutcome", async () => {
  const transport = new FakeTransport();
  const input = {
    parent: "LEL-42",
    summary: "Fix it",
    fields: { customfield_10481: "SERVICE" },
  };
  const preview = await createSubtask(transport, input);
  transport.readbackOverride = () => ({
    key: "LEL-99",
    id: "99",
    fields: {
      project: { id: "10848", key: "LEL" },
      issuetype: { id: "10101", name: "Sub-task" },
      parent: { key: "LEL-100", id: "100" },
      summary: "Fix it",
      customfield_10481: { value: "SERVICE" },
    },
  });
  await assert.rejects(
    createSubtask(transport, { ...input, confirm: preview.confirmation }),
    (error) => {
      assert.ok(error instanceof JiraAmbiguousOutcome);
      assert.equal(error.verification.isExpectedParent, false);
      return true;
    }
  );
});

test("CLI create-subtask keeps file forms mutually exclusive and requires parent", () => {
  for (const args of [
    ["create-subtask", "--parent", "LEL-1", "--summary", "x", "--summary-file", "y"],
    ["create-subtask", "--parent", "LEL-1", "--description", "x", "--description-file", "y"],
  ]) {
    const result = spawnSync(process.execPath, [cli, ...args], { encoding: "utf8" });
    assert.notEqual(result.status, 0, args.join(" "));
    assert.match(`${result.stdout}${result.stderr}`, /mutually exclusive/);
  }
});

function runCreateSubtask(origin, args, env = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, "create-subtask", ...args], {
      cwd: join(here, "..", ".."),
      env: {
        ...process.env,
        JIRA_BASE_URL: origin,
        JIRA_PROTOCOL_TEST_MODE: "1",
        JIRA_SESSION_COOKIE: "loopback-session",
        ...env,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("error", reject);
    child.on("close", (status, signal) => resolve({ status, signal, stdout, stderr }));
  });
}

test("CLI create-subtask --summary-file/--description-file reach the operation with Russian text", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "jira-subtask-cli-ru-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const summaryInner = "Заголовок подзадачи «Проверка»";
  const summaryOnDisk = `  ${summaryInner}  \n`;
  const description = "Описание подзадачи:\n- строка 1\n{noformat}text{noformat}\nКонец.";
  const summaryFile = join(dir, "summary.txt");
  const descriptionFile = join(dir, "description.md");
  await writeFile(summaryFile, summaryOnDisk, "utf8");
  await writeFile(descriptionFile, description, "utf8");
  const requests = [];
  const server = createServer((request, response) => {
    requests.push(`${request.method} ${request.url}`);
    if (request.url.startsWith("/rest/api/2/issue/createmeta")) {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify(createmeta()));
      return;
    }
    if (request.url.startsWith("/rest/api/2/issue/LEL-42")) {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify(parentState()));
      return;
    }
    response.statusCode = 404;
    response.setHeader("content-type", "application/json");
    response.end(JSON.stringify({ errorMessages: ["unexpected fixture path"] }));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const result = await runCreateSubtask(origin, [
    "--parent",
    "LEL-42",
    "--summary-file",
    summaryFile,
    "--description-file",
    descriptionFile,
    "--field",
    "customfield_10481=SERVICE",
  ]);
  assert.equal(result.status, 3, result.stderr || result.stdout);
  const receipt = JSON.parse(result.stdout);
  assert.equal(receipt.state, "AwaitingConfirmation");
  assert.equal(receipt.operation.parentKey, "LEL-42");
  assert.equal(receipt.operation.fields.summary, summaryInner);
  assert.equal(receipt.operation.fields.description, description);
  assert.equal(
    requests.filter((url) => url.includes("/rest/api/2/issue/createmeta")).length,
    1
  );
  assert.equal(
    requests.some((url) => url.startsWith("POST /rest/api/2/issue ")),
    false
  );
});

function createmetaMulti() {
  const base = createmeta();
  base.projects[0].issuetypes.push({
    id: "10340",
    name: "Sub-task",
    subtask: true,
    fields: {
      summary: { name: "Summary", required: true },
      parent: { name: "Parent", required: true },
      project: { name: "Project", required: false },
      issuetype: { name: "Issue Type", required: false },
      description: { name: "Description", required: false },
      customfield_10481: {
        name: "Team",
        required: true,
        allowedValues: [{ id: "SERVICE", value: "SERVICE", name: "SERVICE" }],
      },
    },
  });
  return base;
}

test("subtask with multiple live types stays fail-closed without --issue-type-id", async () => {
  const transport = new FakeTransport({ meta: createmetaMulti() });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Fix it",
      fields: { customfield_10481: "SERVICE" },
    }),
    (error) => {
      assert.match(error.message, /multiple subtask issue types/);
      assert.match(error.message, /10340/);
      assert.match(error.message, /10101/);
      assert.match(error.message, /issue-type-id/);
      return true;
    }
  );
  assert.equal(
    transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
    false
  );
});

test("subtask --issue-type-id selects one live subtask type and binds confirmation", async () => {
  const transport = new FakeTransport({ meta: createmetaMulti() });
  const input = {
    parent: "LEL-42",
    summary: "Fix it",
    fields: { customfield_10481: "SERVICE" },
    issueTypeId: "10340",
  };
  const preview = await createSubtask(transport, input);
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.issueTypeId, "10340");
  assert.equal(preview.model.subtaskType.id, "10340");
  const other = await createSubtask(transport, { ...input, issueTypeId: "10101" });
  assert.notEqual(other.confirmation, preview.confirmation);
  const result = await createSubtask(transport, { ...input, confirm: preview.confirmation });
  assert.equal(result.state, "Completed");
  assert.equal(result.readback.issueTypeId, "10340");
  assert.equal(transport.created.issuetype.id, "10340");
});

test("subtask rejects an --issue-type-id that is not a live subtask type", async () => {
  for (const bad of ["10100", "99999"]) {
    const transport = new FakeTransport({ meta: createmetaMulti() });
    await assert.rejects(
      createSubtask(transport, {
        parent: "LEL-42",
        summary: "Fix it",
        fields: { customfield_10481: "SERVICE" },
        issueTypeId: bad,
      }),
      /not a live LEL subtask issue type/
    );
    assert.equal(
      transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
      false
    );
  }
  const single = new FakeTransport();
  await assert.rejects(
    createSubtask(single, {
      parent: "LEL-42",
      summary: "Fix it",
      fields: { customfield_10481: "SERVICE" },
      issueTypeId: "10340",
    }),
    /not a live LEL subtask issue type/
  );
});

test("subtask rejects a malformed --issue-type-id before any write", async () => {
  for (const bad of ["abc", "0", "-3", "10.5"]) {
    const transport = new FakeTransport({ meta: createmetaMulti() });
    await assert.rejects(
      createSubtask(transport, {
        parent: "LEL-42",
        summary: "Fix it",
        fields: { customfield_10481: "SERVICE" },
        issueTypeId: bad,
      }),
      /issue-type-id must be a positive integer/
    );
    assert.equal(
      transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
      false
    );
  }
});

test("subtask refuses a parent matching any live subtask type, not only the selected one", async () => {
  const transport = new FakeTransport({
    meta: createmetaMulti(),
    parent: parentState({ issuetype: { id: "10340", name: "Sub-task" } }),
  });
  await assert.rejects(
    createSubtask(transport, {
      parent: "LEL-42",
      summary: "Fix it",
      fields: { customfield_10481: "SERVICE" },
      issueTypeId: "10101",
    }),
    /cannot have a child subtask/
  );
  assert.equal(
    transport.calls.some((c) => c.method === "POST" && c.path === "/rest/api/2/issue"),
    false
  );
});
