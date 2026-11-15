import { createHash } from "node:crypto";

export class JiraProtocolError extends Error {
  constructor(message, { status, details } = {}) {
    super(message);
    this.name = "JiraProtocolError";
    this.status = status;
    this.details = details;
  }
}

export class JiraAmbiguousOutcome extends Error {
  constructor(message, options = {}) {
    super(message, options);
    this.name = "JiraAmbiguousOutcome";
  }
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function confirmationDigest(operation) {
  return createHash("sha256").update(canonicalJson(operation)).digest("hex");
}

const SECRET_KEY =
  /^(?:atl_?token|formtoken|authorization|cookie|set-cookie)$/i;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const MAX_REDIRECTS = 5;

function redactTokenInputs(value) {
  return value.replace(/<input\b[^>]*>/gi, (tag) => {
    const identifiesToken =
      /(?:\s)(?:name|id)\s*=\s*(?:"(?:atl_?token|formtoken)"|'(?:atl_?token|formtoken)'|(?:atl_?token|formtoken)(?=\s|\/?\s*>))/i.test(
        tag
      );
    if (!identifiesToken) return tag;
    return tag.replace(
      /(\svalue\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s>]+)/i,
      '$1"[REDACTED]"'
    );
  });
}

export function redactSecrets(value) {
  if (Array.isArray(value)) return value.map(redactSecrets);
  if (value && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, entry]) => [
        key,
        SECRET_KEY.test(key) ? "[REDACTED]" : redactSecrets(entry),
      ])
    );
  if (typeof value !== "string") return value;
  return redactTokenInputs(value).replace(
    /([?&][^?&#=\s"'<>]*(?:token|auth|session|cookie)[^?&#=\s"'<>]*=)[^&#\s"'<>]*/gi,
    "$1[REDACTED]"
  );
}

function responseDetails(payload) {
  if (!payload || typeof payload !== "object") return undefined;
  const errors = payload.errors;
  const messages = payload.errorMessages;
  return {
    ...(errors && typeof errors === "object" ? { errors } : {}),
    ...(Array.isArray(messages) ? { errorMessages: messages } : {}),
  };
}

export class JiraTransport {
  constructor({
    origin = "https://jira3.tcsbank.ru",
    cookie,
    atlToken,
    timeoutMs = 20_000,
    fetchImpl = globalThis.fetch,
    allowTestOrigin = false,
  } = {}) {
    if (!cookie) throw new JiraProtocolError("JIRA_SESSION_COOKIE is required");
    let parsed;
    try {
      parsed = new URL(origin);
    } catch {
      throw new JiraProtocolError("JIRA_BASE_URL must be an absolute URL");
    }
    const productionOrigin = "https://jira3.tcsbank.ru";
    const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);
    const allowedTestOrigin =
      allowTestOrigin &&
      /^https?:$/.test(parsed.protocol) &&
      loopback.has(parsed.hostname);
    if (parsed.origin !== productionOrigin && !allowedTestOrigin)
      throw new JiraProtocolError(
        "Jira origin must be https://jira3.tcsbank.ru; test mode permits loopback origins only"
      );
    this.origin = parsed.origin;
    this.cookie = cookie;
    this.atlToken = atlToken;
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
  }

  url(path, query = {}) {
    const url = new URL(path, this.origin);
    if (url.origin !== this.origin)
      throw new JiraProtocolError(
        "Jira request URL must stay on the configured origin"
      );
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null)
        url.searchParams.set(key, String(value));
    }
    return url;
  }

  async request({
    method = "GET",
    path,
    query,
    json,
    form,
    expect = "json",
    mutation = false,
  }) {
    const url = this.url(path, query);
    const headers = {
      accept:
        expect === "json"
          ? "application/json, text/javascript, */*; q=0.01"
          : "text/html, application/json;q=0.9, */*;q=0.8",
      cookie: this.cookie,
      "x-requested-with": "XMLHttpRequest",
      "x-atlassian-greenhopper-gadget": "false",
    };
    let body;
    if (json !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(json);
    } else if (form !== undefined) {
      headers["content-type"] =
        "application/x-www-form-urlencoded; charset=UTF-8";
      body = form instanceof URLSearchParams ? form.toString() : String(form);
    }
    let response;
    let requestUrl = url;
    let requestMethod = method;
    let requestBody = body;
    let followedRedirect = false;
    try {
      for (let redirects = 0; ; redirects += 1) {
        if (requestUrl.origin !== this.origin)
          throw new JiraProtocolError(
            "Jira redirect left the configured origin"
          );
        response = await this.fetchImpl(requestUrl, {
          method: requestMethod,
          headers,
          body: requestBody,
          redirect: "manual",
          signal: AbortSignal.timeout(this.timeoutMs),
        });
        if (response.url && new URL(response.url).origin !== this.origin)
          throw new JiraProtocolError("Jira response came from another origin");
        if (!REDIRECT_STATUSES.has(response.status)) break;
        const location = response.headers.get("location");
        if (!location) break;
        if (redirects >= MAX_REDIRECTS)
          throw new JiraProtocolError("Jira redirect limit exceeded");
        const nextUrl = new URL(location, requestUrl);
        if (nextUrl.origin !== this.origin)
          throw new JiraProtocolError(
            "Jira redirect left the configured origin"
          );
        if (
          response.status === 303 ||
          ((response.status === 301 || response.status === 302) &&
            requestMethod === "POST")
        ) {
          requestMethod = "GET";
          requestBody = undefined;
        }
        requestUrl = nextUrl;
        followedRedirect = true;
      }
    } catch (error) {
      if (error instanceof JiraProtocolError && !mutation) throw error;
      const ErrorType = mutation ? JiraAmbiguousOutcome : JiraProtocolError;
      throw new ErrorType(
        mutation
          ? "transport failed after a Jira write may have started"
          : "Jira request failed before a readable response",
        { cause: error }
      );
    }

    let text;
    try {
      text = await response.text();
    } catch (error) {
      const ErrorType = mutation ? JiraAmbiguousOutcome : JiraProtocolError;
      throw new ErrorType("Jira response body could not be read", {
        cause: error,
      });
    }
    let payload;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    } else {
      payload = null;
    }

    if (!response.ok) {
      if (mutation && (followedRedirect || response.status >= 500))
        throw new JiraAmbiguousOutcome(
          `Jira returned HTTP ${response.status} after a write may have started`
        );
      throw new JiraProtocolError(`Jira returned HTTP ${response.status}`, {
        status: response.status,
        details: responseDetails(payload),
      });
    }
    if (
      expect === "json" &&
      (payload === null || typeof payload !== "object")
    ) {
      const ErrorType = mutation ? JiraAmbiguousOutcome : JiraProtocolError;
      throw new ErrorType(
        mutation
          ? "Jira returned an unreadable success response after a write may have completed"
          : "Jira returned a non-JSON response"
      );
    }
    return { payload, response };
  }
}
