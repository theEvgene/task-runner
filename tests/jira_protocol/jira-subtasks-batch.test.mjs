import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import test from "node:test";
import { serializeReceipt } from "../../scripts/jira-protocol.mjs";
import {
  JiraProtocolError,
} from "../../scripts/jira_protocol/protocol.mjs";
import { assertHandoffClean } from "../../scripts/jira_protocol/handoff.mjs";
import {
  BATCH_MAX_ITEMS,
  createSubtaskBatch,
  normalizeSummary,
  parseBatchFile,
  planSubtaskBatch,
} from "../../scripts/jira_protocol/batch.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const cli = join(here, "..", "..", "scripts", "jira-protocol.mjs");

function createmeta(extraFields = {}) {
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
              ...extraFields,
            },
          },
        ],
      },
    ],
  };
}

class BatchFake {
  constructor({ children = [], metaFields = {}, deny = null, deadReads = [] } = {}) {
    this.children = children.map((entry) => ({ ...entry }));
    this.metaFields = metaFields;
    this.deny = deny;
    this.deadReads = new Set(deadReads);
    this.posts = [];
    this.counter = 100;
    this.created = new Map();
  }

  async request(request) {
    if (request.path === "/rest/api/2/issue/createmeta")
      return { payload: structuredClone(createmeta(this.metaFields)) };
    if (request.method === "POST" && request.path === "/rest/api/2/issue") {
      const fields = structuredClone(request.json?.fields ?? {});
      this.posts.push(fields);
      if (this.deny) throw this.deny;
      this.counter += 1;
      const key = `LEL-${this.counter}`;
      this.created.set(key, fields);
      return { payload: { key, id: String(this.counter) } };
    }
    if (request.path === "/rest/api/2/issue/LEL-42")
      return {
        payload: {
          key: "LEL-42",
          id: "42",
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10100", name: "Task" },
            subtasks: structuredClone(this.children),
          },
        },
      };
    const createdMatch = /^\/rest\/api\/2\/issue\/(LEL-\d+)$/.exec(request.path ?? "");
    if (createdMatch) {
      const key = createdMatch[1];
      if (this.deadReads.has(key))
        throw new JiraProtocolError("fixture readback offline");
      const fields = this.created.get(key);
      if (!fields) throw new Error(`unknown created key ${key}`);
      return {
        payload: {
          key,
          id: key.slice(4),
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10101", name: "Sub-task" },
            parent: { key: "LEL-42", id: "42" },
            summary: fields.summary,
            ...(fields.description !== undefined
              ? { description: fields.description }
              : {}),
          },
        },
      };
    }
    throw new Error(`unexpected request ${request.method ?? "GET"} ${request.path}`);
  }
}

function items(summaries) {
  return summaries.map((summary) => ({ summary, normalized: normalizeSummary(summary) }));
}

test("batch file parsing keeps multiline UTF-8 and rejects bad shapes", () => {
  const parsed = parseBatchFile(
    JSON.stringify([
      { summary: "  Заголовок «проверка»  ", description: "Строка 1\nСтрока 2\nКонец." },
      { summary: "Second", fields: { customfield_1: "x" } },
    ])
  );
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0].summary, "Заголовок «проверка»");
  assert.equal(parsed[0].description, "Строка 1\nСтрока 2\nКонец.");
  assert.deepEqual(parsed[1].fields, { customfield_1: "x" });
  for (const bad of [
    "not json",
    "{}",
    "[]",
    JSON.stringify([{ summary: "   " }]),
    JSON.stringify([{ description: "no summary" }]),
    JSON.stringify([{ summary: "x", fields: ["not-object"] }]),
    JSON.stringify(
      Array.from({ length: BATCH_MAX_ITEMS + 1 }, (_, index) => ({ summary: `s${index}` }))
    ),
  ])
    assert.throws(() => parseBatchFile(bad), JiraProtocolError);
});

test("read-only plan shows matches and performs no write", async () => {
  const transport = new BatchFake({
    children: [{ key: "LEL-7", fields: { summary: "Done already" } }],
  });
  const preview = await planSubtaskBatch(transport, {
    parent: "lel-42",
    items: items(["Done already", "Fresh work"]),
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.operation.parentKey, "LEL-42");
  assert.equal(preview.operation.issueTypeId, "10101");
  assert.deepEqual(
    preview.items.map((entry) => entry.match),
    ["exists", "new"]
  );
  assert.deepEqual(preview.items[0].existingKeys, ["LEL-7"]);
  assert.equal(transport.posts.length, 0);
});

test("confirmed batch creates only new entries with parent linkage", async () => {
  const transport = new BatchFake({
    children: [{ key: "LEL-7", fields: { summary: "Done already" } }],
  });
  const planned = items(["Done already", "Fresh work", "Третья задача"]);
  const preview = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  const done = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.writes, 2);
  assert.equal(transport.posts.length, 2);
  for (const post of transport.posts) {
    assert.equal(post.project.id, "10848");
    assert.equal(post.parent.key, "LEL-42");
    assert.equal(post.issuetype.id, "10101");
  }
  assert.equal(transport.posts[0].summary, "Fresh work");
  assert.equal(transport.posts[1].summary, "Третья задача");
  const byIndex = new Map(done.results.map((entry) => [entry.index, entry]));
  assert.equal(byIndex.get(1).state, "AlreadyExists");
  assert.equal(byIndex.get(1).key, "LEL-7");
  assert.equal(byIndex.get(2).state, "Created");
  assert.match(byIndex.get(2).link, /^https:\/\/jira3\.tcsbank\.ru\/browse\/LEL-/);
  assert.equal(assertHandoffClean(done.handoff), true);
  assert.doesNotMatch(serializeReceipt(done), /cookie/i);
});

test("repeat run after success creates nothing", async () => {
  const transport = new BatchFake({
    children: [
      { key: "LEL-101", fields: { summary: "One" } },
      { key: "LEL-102", fields: { summary: "Two" } },
    ],
  });
  const done = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: items(["One", "Two"]),
  });
  assert.equal(done.state, "Completed");
  assert.equal(done.writes, 0);
  assert.equal(transport.posts.length, 0);
  assert.ok(done.results.every((entry) => entry.state === "AlreadyExists"));
});

test("ambiguous matches need an explicit decision and stay unwritten", async () => {
  const transport = new BatchFake({
    children: [
      { key: "LEL-7", fields: { summary: "Same name" } },
      { key: "LEL-8", fields: { summary: "Same name" } },
    ],
  });
  const planned = items(["Same name", "Other"]);
  const preview = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
  });
  assert.equal(preview.state, "AwaitingConfirmation");
  assert.equal(preview.items[0].match, "ambiguous");
  await assert.rejects(
    createSubtaskBatch(transport, {
      parent: "LEL-42",
      items: planned,
      confirm: preview.confirmation,
    }),
    (error) => {
      assert.match(error.message, /explicit --allow-duplicate decision/);
      assert.equal(error.details.blocked[0].summary, "Same name");
      return true;
    }
  );
  assert.equal(transport.posts.length, 0);
  const allowed = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    allowDuplicates: ["Same name"],
    confirm: (
      await createSubtaskBatch(transport, {
        parent: "LEL-42",
        items: planned,
        allowDuplicates: ["Same name"],
      })
    ).confirmation,
  });
  assert.equal(allowed.state, "Completed");
  assert.equal(allowed.writes, 2);
});

test("identical file entries stay ambiguous even when allowed", async () => {
  const transport = new BatchFake({ children: [] });
  const planned = items(["Twin", "Twin"]);
  const preview = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    allowDuplicates: ["Twin"],
  });
  assert.ok(preview.items.every((entry) => entry.match === "ambiguous"));
  await assert.rejects(
    createSubtaskBatch(transport, {
      parent: "LEL-42",
      items: planned,
      allowDuplicates: ["Twin"],
      confirm: preview.confirmation,
    }),
    /explicit --allow-duplicate decision/
  );
  assert.equal(transport.posts.length, 0);
});

test("unknown entry outcome stops that entry only, with one attempt", async () => {
  const transport = new BatchFake({ children: [] });
  const planned = items(["First", "Broken", "Last"]);
  const preview = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
  });
  transport.deadReads.add("LEL-102");
  const done = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "UnknownOutcome");
  const byIndex = new Map(done.results.map((entry) => [entry.index, entry]));
  assert.equal(byIndex.get(1).state, "Created");
  assert.equal(byIndex.get(2).state, "UnknownOutcome");
  assert.equal(byIndex.get(2).key, "LEL-102");
  assert.equal(byIndex.get(3).state, "Created");
  assert.deepEqual(
    transport.posts.map((post) => post.summary),
    ["First", "Broken", "Last"]
  );
});

test("HTTP 401 aborts the batch as terminal", async () => {
  const transport = new BatchFake({
    children: [],
    deny: new JiraProtocolError("Jira returned HTTP 401", { status: 401 }),
  });
  const planned = items(["First", "Second"]);
  const preview = await planSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
  });
  await assert.rejects(
    createSubtaskBatch(transport, {
      parent: "LEL-42",
      items: planned,
      confirm: preview.confirmation,
    }),
    (error) => {
      assert.equal(error.status, 401);
      return true;
    }
  );
  assert.equal(transport.posts.length, 1);
});

test("missing live required fields return MissingRequiredFields without writes", async () => {
  const transport = new BatchFake({
    children: [],
    metaFields: { customfield_10481: { name: "Team", required: true } },
  });
  const result = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: items(["Needs team"]),
  });
  assert.equal(result.state, "MissingRequiredFields");
  assert.deepEqual(result.model.requiredFields, [
    { id: "summary", label: "Summary" },
    { id: "customfield_10481", label: "Team" },
  ]);
  assert.deepEqual(
    result.items[0].missingFields,
    [{ id: "customfield_10481", label: "Team" }]
  );
  assert.equal(transport.posts.length, 0);
});

test("unverifiable existing children fail the batch closed", async () => {
  const transport = new BatchFake({ children: [] });
  transport.request = async (request) => {
    if (request.path === "/rest/api/2/issue/createmeta")
      return { payload: structuredClone(createmeta()) };
    if (request.path === "/rest/api/2/issue/LEL-42")
      return {
        payload: {
          key: "LEL-42",
          id: "42",
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10100", name: "Task" },
          },
        },
      };
    throw new Error(`unexpected request ${request.path}`);
  };
  await assert.rejects(
    planSubtaskBatch(transport, { parent: "LEL-42", items: items(["x"]) }),
    (error) => {
      assert.match(error.message, /matches are unverified/);
      assert.equal(error.stage, "batch_existing");
      return true;
    }
  );
  assert.equal(transport.posts.length, 0);
});

test("parent outside LEL or a subtask is rejected before any write", async () => {
  for (const fields of [
    { project: { id: "99999", key: "OTHER" }, issuetype: { id: "10100", name: "Task" } },
    { project: { id: "10848", key: "LEL" }, issuetype: { id: "10101", name: "Sub-task" } },
  ]) {
    const transport = new BatchFake({ children: [] });
    const original = transport.request.bind(transport);
    transport.request = async (request) => {
      if (request.path === "/rest/api/2/issue/LEL-42") {
        const response = await original(request);
        response.payload.fields = { ...response.payload.fields, ...fields };
        return response;
      }
      return original(request);
    };
    await assert.rejects(
      planSubtaskBatch(transport, { parent: "LEL-42", items: items(["x"]) }),
      JiraProtocolError
    );
    assert.equal(transport.posts.length, 0);
  }
});

test("CLI rejects a missing batch file without a session", () => {
  const result = spawnSync(
    process.execPath,
    [cli, "create-subtasks", "--parent", "LEL-1"],
    { encoding: "utf8" }
  );
  assert.equal(result.status, 2);
  assert.match(`${result.stdout}${result.stderr}`, /--file is required/);
});

test("CLI rejects an unreadable batch file", () => {
  const result = spawnSync(
    process.execPath,
    [cli, "create-subtasks", "--parent", "LEL-1", "--file", "no-such-batch.json"],
    { encoding: "utf8" }
  );
  assert.equal(result.status, 2);
  assert.match(`${result.stdout}${result.stderr}`, /--file is unreadable/);
});

test("unknown item outcomes keep no secret and no retry marker", async () => {
  const transport = new BatchFake({ children: [] });
  transport.deadReads.add("LEL-101");
  const preview = await planSubtaskBatch(transport, {
    parent: "LEL-42",
    items: items(["Solo"]),
  });
  const done = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: items(["Solo"]),
    confirm: preview.confirmation,
  });
  assert.equal(done.state, "UnknownOutcome");
  assert.equal(done.results[0].state, "UnknownOutcome");
  assert.equal(done.results[0].key, "LEL-101");
  assert.equal(transport.posts.length, 1);
  assert.equal(assertHandoffClean(done), true);
});
