import { JiraTransport } from "../../../scripts/jira_protocol/protocol.mjs";

const TEST_ORIGIN = "http://127.0.0.1:9123";

// Boundary: scripted operation transports below replay operation-level
// evidence in request order. They do NOT exercise JiraTransport parsing;
// use createUnreadableMutationTransport for the real parsing path.

export function createUnreadableMutationTransport({
  status = 200,
  body = "",
  brokenBody = false,
} = {}) {
  const requests = [];
  const transport = new JiraTransport({
    origin: TEST_ORIGIN,
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async (url, options) => {
      requests.push({ url: String(url), options });
      if (brokenBody)
        return {
          status,
          ok: status >= 200 && status < 300,
          url: "",
          text: async () => {
            throw new Error("broken");
          },
        };
      return new Response(body, { status });
    },
  });
  return { transport, requests };
}

export function createSequencedTransport(steps, { atlToken } = {}) {
  const calls = [];
  const queue = [...steps];
  const transport = {
    ...(atlToken !== undefined ? { atlToken } : {}),
    calls,
    async request(request) {
      calls.push(request);
      const step = queue.shift();
      if (!step) throw new Error(`unexpected request ${request.path}`);
      if (step.error) throw step.error;
      return step.response;
    },
  };
  return {
    transport,
    calls,
    mutationCount() {
      return calls.filter((call) => call.mutation).length;
    },
    requestOrder() {
      return calls.map(
        (call) => `${String(call.method ?? "GET").toUpperCase()} ${call.path}`
      );
    },
  };
}

export function assertSingleMutation(calls) {
  const count = calls.filter((call) => call.mutation).length;
  if (count !== 1)
    throw new Error(`expected exactly one mutation, saw ${count}`);
}
