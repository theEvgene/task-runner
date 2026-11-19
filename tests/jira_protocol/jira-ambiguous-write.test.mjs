import assert from "node:assert/strict";
import test from "node:test";
import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
} from "../../scripts/jira_protocol/protocol.mjs";
import {
  assertSingleMutation,
  createSequencedTransport,
  createUnreadableMutationTransport,
} from "./helpers/ambiguous-write.mjs";

test("unreadable empty 2xx uses the real JiraTransport parsing path", async () => {
  const { transport, requests } = createUnreadableMutationTransport({
    status: 200,
    body: "",
  });
  await assert.rejects(
    transport.request({ method: "PUT", path: "/write", mutation: true }),
    JiraAmbiguousOutcome
  );
  assert.equal(requests.length, 1);
  assert.equal(new URL(requests[0].url).pathname, "/write");
});

test("sequenced operation models one mutation plus successful or failed reads", async () => {
  const { transport, calls, mutationCount, requestOrder } =
    createSequencedTransport([
      { response: { payload: { ok: true } } },
      { response: { payload: { key: "LEL-42" } } },
    ]);
  const first = await transport.request({
    method: "POST",
    path: "/write",
    mutation: true,
  });
  assert.deepEqual(first.payload, { ok: true });
  const second = await transport.request({ path: "/rest/api/2/issue/LEL-42" });
  assert.deepEqual(second.payload, { key: "LEL-42" });
  assert.deepEqual(requestOrder(), [
    "POST /write",
    "GET /rest/api/2/issue/LEL-42",
  ]);
  assert.equal(mutationCount(), 1);
  assertSingleMutation(calls);

  const failed = createSequencedTransport([
    { response: { payload: { ok: true } } },
    { error: new JiraProtocolError("readback failed") },
  ]);
  await failed.transport.request({
    method: "POST",
    path: "/write",
    mutation: true,
  });
  await assert.rejects(
    failed.transport.request({ path: "/rest/api/2/issue/LEL-42" }),
    /readback failed/
  );
  assert.equal(failed.mutationCount(), 1);
  assertSingleMutation(failed.calls);
});
