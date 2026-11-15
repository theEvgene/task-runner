import assert from "node:assert/strict";
import test from "node:test";
import {
  JiraAmbiguousOutcome,
  JiraTransport,
} from "../../scripts/jira_protocol/protocol.mjs";

test("post-write redirects and unreadable bodies retain unknown outcome", async () => {
  let calls = 0;
  const transport = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async () =>
      ++calls === 1
        ? new Response(null, { status: 303, headers: { location: "/missing" } })
        : new Response("missing", { status: 404 }),
  });
  await assert.rejects(
    transport.request({ method: "POST", path: "/write", mutation: true }),
    JiraAmbiguousOutcome
  );
  const broken = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async () => ({
      status: 200,
      ok: true,
      url: "",
      text: async () => {
        throw new Error("broken");
      },
    }),
  });
  await assert.rejects(
    broken.request({ method: "POST", path: "/write", mutation: true }),
    JiraAmbiguousOutcome
  );
});

test("transport follows same-origin redirects and keeps credentials on that origin", async () => {
  const calls = [];
  const transport = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      if (calls.length === 1)
        return new Response(null, {
          status: 302,
          headers: { location: "/redirected" },
        });
      return Response.json({ ok: true });
    },
  });

  const result = await transport.request({ path: "/start" });

  assert.deepEqual(result.payload, { ok: true });
  assert.deepEqual(
    calls.map((call) => new URL(call.url).pathname),
    ["/start", "/redirected"]
  );
  assert.equal(
    calls.every((call) => call.options.redirect === "manual"),
    true
  );
  assert.equal(
    calls.every((call) => call.options.headers.cookie === "fixture-cookie"),
    true
  );
});

test("transport refuses foreign redirects and foreign final response origins", async () => {
  const redirectCalls = [];
  const redirecting = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async (url, options) => {
      redirectCalls.push({ url: String(url), options });
      return new Response(null, {
        status: 302,
        headers: { location: "https://foreign.invalid/stolen" },
      });
    },
  });
  await assert.rejects(
    redirecting.request({ path: "/start" }),
    /redirect left the configured origin/
  );
  assert.equal(redirectCalls.length, 1);
  await assert.rejects(
    redirecting.request({ method: "POST", path: "/start", mutation: true }),
    JiraAmbiguousOutcome
  );

  const foreign = new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
  Object.defineProperty(foreign, "url", {
    value: "https://foreign.invalid/rest/api/2/issue/LEL-42",
  });
  const finalOrigin = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async () => foreign,
  });
  await assert.rejects(
    finalOrigin.request({ path: "/rest/api/2/issue/LEL-42" }),
    /response came from another origin/
  );
});

test("transport refuses an absolute foreign request before sending credentials", async () => {
  let called = false;
  const transport = new JiraTransport({
    origin: "http://127.0.0.1:9123",
    cookie: "fixture-cookie",
    allowTestOrigin: true,
    fetchImpl: async () => {
      called = true;
      return Response.json({});
    },
  });
  await assert.rejects(
    transport.request({ path: "https://foreign.invalid/steal" }),
    /request URL must stay on the configured origin/
  );
  assert.equal(called, false);
});
