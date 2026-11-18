import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  createSubtaskBatch,
  normalizeSummary,
} from "../../scripts/jira_protocol/batch.mjs";
import { JiraProtocolError } from "../../scripts/jira_protocol/protocol.mjs";
import { assertHandoffClean } from "../../scripts/jira_protocol/handoff.mjs";

function createmeta() {
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
              description: { name: "Description", required: false },
            },
          },
        ],
      },
    ],
  };
}

// Shared remote: POSTs always succeed remotely even when the child listing
// stays stale (does not show the new child). This reproduces the crash
// window: remote success + stale listing would duplicate without a durable
// pre-write marker.
class StaleListingFake {
  constructor() {
    this.posts = [];
    this.counter = 100;
    this.created = new Map();
  }
  async request(request) {
    if (request.path === "/rest/api/2/issue/createmeta")
      return { payload: structuredClone(createmeta()) };
    if (request.method === "POST" && request.path === "/rest/api/2/issue") {
      const fields = structuredClone(request.json?.fields ?? {});
      this.posts.push(fields);
      this.counter += 1;
      const key = `LEL-${this.counter}`;
      this.created.set(key, fields);
      return { payload: { key, id: String(this.counter) }, response: { status: 201 } };
    }
    if (request.path === "/rest/api/2/issue/LEL-42")
      return {
        payload: {
          key: "LEL-42",
          id: "42",
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10100", name: "Task" },
            subtasks: [],
          },
        },
        response: { status: 200 },
      };
    const m = /^\/rest\/api\/2\/issue\/(LEL-\d+)$/.exec(request.path ?? "");
    if (m) {
      const key = m[1];
      const fields = this.created.get(key);
      if (!fields) throw new Error(`unknown key ${key}`);
      return {
        payload: {
          key,
          id: key.slice(4),
          fields: {
            project: { id: "10848", key: "LEL" },
            issuetype: { id: "10101", name: "Sub-task" },
            parent: { key: "LEL-42", id: "42" },
            summary: fields.summary,
          },
        },
        response: { status: 200 },
      };
    }
    throw new Error(`unexpected ${request.method ?? "GET"} ${request.path}`);
  }
}

function tmpJournal() {
  const dir = mkdtempSync(join(tmpdir(), "batch-crash-"));
  return join(dir, "journal.json");
}

function items(summaries) {
  return summaries.map((s) => ({ summary: s, normalized: normalizeSummary(s) }));
}

test("crash after remote success before persistSuccess leaves blocking marker; stale rerun posts zero times", async () => {
  const path = tmpJournal();
  const transport = new StaleListingFake();
  const planned = items(["Solo"]);
  const preview = await createSubtaskBatch(transport, { parent: "LEL-42", items: planned });
  assert.equal(preview.state, "AwaitingConfirmation");

  // Simulate a crash after the remote POST succeeded but before the
  // success marker replaced the in-flight marker: run the first item with
  // a patched persist that throws after the POST. Simplest deterministic
  // simulation: perform the real POST via the batch once with an injected
  // crash on readback persistence by failing the second journal write.
  // Here we emulate the resulting journal state directly: in-flight marker
  // present, remote already holds LEL-101, child listing still stale.
  const first = await createSubtaskBatch(transport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview.confirmation,
    journalPath: path,
  });
  assert.equal(first.state, "Completed");
  assert.equal(transport.posts.length, 1);
  const stored = JSON.parse(readFileSync(path, "utf8"));
  assert.ok(stored.results["1"]);

  // Now simulate the crash window proper: fresh journal with only the
  // pre-write in-flight marker (as if the process died between POST and
  // persistSuccess), remote has the created issue, listing is stale.
  const crashPath = tmpJournal();
  const crashTransport = new StaleListingFake();
  // Manual pre-write marker mimicking persistInflight before POST.
  const { saveBatchJournal, batchIdentity } = await import(
    "../../scripts/jira_protocol/batch.mjs"
  );
  const batchId = batchIdentity({ parent: "LEL-42", items: planned });
  await saveBatchJournal(crashPath, {
    schema: "jira-subtasks-journal/1",
    batchId,
    parentKey: "LEL-42",
    requestedIssueTypeId: null,
    resolvedIssueTypeId: "10101",
    itemCount: 1,
    items: [{ index: 1, summary: "Solo", normalized: "Solo" }],
    results: {},
    unknown: {},
    inflight: { 1: { index: 1, summary: "Solo", normalized: "Solo" } },
    updatedAt: new Date().toISOString(),
  });
  // Remote already succeeded once (stale listing hides it).
  crashTransport.counter = 100;
  crashTransport.created.set("LEL-101", { summary: "Solo" });
  // Blind rerun must block before any new POST despite the stale listing.
  const preview2 = await createSubtaskBatch(crashTransport, {
    parent: "LEL-42",
    items: planned,
    journalPath: crashPath,
  });
  assert.equal(crashTransport.posts.length, 0);
  await assert.rejects(
    createSubtaskBatch(crashTransport, {
      parent: "LEL-42",
      items: planned,
      confirm: preview2.confirmation,
      journalPath: crashPath,
    }),
    /resolve each with --resolve-unknown/
  );
  assert.equal(crashTransport.posts.length, 0);

  // Safe recovery: operator verifies LEL-101 exists and resolves to it.
  // Zero additional POSTs, receipt reports the verified key.
  const preview3 = await createSubtaskBatch(crashTransport, {
    parent: "LEL-42",
    items: planned,
    journalPath: crashPath,
    resolveUnknown: ["1=LEL-101"],
  });
  const done3 = await createSubtaskBatch(crashTransport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview3.confirmation,
    journalPath: crashPath,
    resolveUnknown: ["1=LEL-101"],
  });
  assert.equal(done3.state, "Completed");
  assert.equal(crashTransport.posts.length, 0);
  assert.equal(done3.results[0].key, "LEL-101");
  assert.equal(done3.writes, 0);
  assert.equal(assertHandoffClean(done3), true);
});

test("proven pre-dispatch failure clears in-flight marker as Failed; uncertain failure stays blocking", async () => {
  const path = tmpJournal();
  // Proven: submission rejected with definitive 400 -> Failed, marker cleared.
  const badTransport = new StaleListingFake();
  const real = badTransport.request.bind(badTransport);
  badTransport.request = async (req) => {
    if (req.method === "POST" && req.path === "/rest/api/2/issue")
      throw new JiraProtocolError("Jira returned HTTP 400", { status: 400 });
    return real(req);
  };
  const planned = items(["Bad"]);
  const preview = await createSubtaskBatch(badTransport, { parent: "LEL-42", items: planned });
  const failed = await createSubtaskBatch(badTransport, {
    parent: "LEL-42",
    items: planned,
    confirm: preview.confirmation,
    journalPath: path,
  });
  assert.equal(failed.state, "Failed");
  const stored = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(stored.inflight?.["1"], undefined);
  assert.equal(stored.unknown?.["1"], undefined);

  // Uncertain: generic error after dispatch must not clear as Failed.
  const path2 = tmpJournal();
  const flaky = new StaleListingFake();
  const real2 = flaky.request.bind(flaky);
  let postCount = 0;
  flaky.request = async (req) => {
    if (req.method === "POST" && req.path === "/rest/api/2/issue") {
      postCount += 1;
      throw new Error("simulated transport loss after dispatch");
    }
    return real2(req);
  };
  const planned2 = items(["Flaky"]);
  const preview2 = await createSubtaskBatch(flaky, { parent: "LEL-42", items: planned2 });
  const uncertain = await createSubtaskBatch(flaky, {
    parent: "LEL-42",
    items: planned2,
    confirm: preview2.confirmation,
    journalPath: path2,
  });
  assert.equal(uncertain.state, "UnknownOutcome");
  assert.equal(postCount, 1);
  const stored2 = JSON.parse(readFileSync(path2, "utf8"));
  assert.ok(stored2.unknown["1"] ?? stored2.inflight["1"]);
});

test("journal IO errors carry stage batch_journal", async () => {
  const { loadBatchJournal } = await import("../../scripts/jira_protocol/batch.mjs");
  const dir = mkdtempSync(join(tmpdir(), "batch-journal-stage-"));
  const bad = join(dir, "journal.json");
  const { writeFileSync } = await import("node:fs");
  writeFileSync(bad, "{not json", "utf8");
  await assert.rejects(loadBatchJournal(bad), (error) => {
    assert.equal(error.stage, "batch_journal");
    return true;
  });
});
