import { randomUUID } from "node:crypto";

export class ProtocolError extends Error {}
export class AmbiguousOutcome extends Error {}

export class HttpRpcTransport {
  constructor(tokens, timeoutMs = 20_000) {
    this.tokens = tokens;
    this.timeoutMs = timeoutMs;
  }

  async call(target, method, params) {
    const token = this.tokens[target.environment];
    if (!token)
      throw new ProtocolError(`missing bearer token for ${target.environment}`);
    const id = randomUUID();
    let response;
    try {
      response = await fetch(
        `${target.origin}/feature-toggle/api/admin/v1/${method}`,
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            authorization: `Bearer ${token}`,
          },
          body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
          signal: AbortSignal.timeout(this.timeoutMs),
        }
      );
    } catch (error) {
      throw new AmbiguousOutcome(
        `transport outcome unknown for ${target.environment}`,
        { cause: error }
      );
    }
    if (!response.ok) {
      if (response.status >= 500) {
        throw new AmbiguousOutcome(
          `HTTP ${response.status} left the ${target.environment} outcome unknown`
        );
      }
      throw new ProtocolError(
        `HTTP ${response.status} from ${target.environment}`
      );
    }
    let payload;
    try {
      payload = await response.json();
    } catch (error) {
      throw new AmbiguousOutcome(
        `unreadable RPC response from ${target.environment}`,
        { cause: error }
      );
    }
    if (
      !payload ||
      typeof payload !== "object" ||
      payload.id !== id ||
      payload.jsonrpc !== "2.0"
    ) {
      throw new AmbiguousOutcome(
        `unbound RPC response from ${target.environment}`
      );
    }
    if ("error" in payload)
      throw new ProtocolError(
        `RPC error ${payload.error?.code}: ${payload.error?.message}`
      );
    if (!("result" in payload))
      throw new AmbiguousOutcome(
        `RPC result missing from ${target.environment}`
      );
    return payload.result;
  }
}

export async function findToggle(
  transport,
  target,
  parentId,
  key,
  entityPath,
  maxPages = 100
) {
  let offset = 0;
  const limit = 10; // Exact page size from the observed request.
  for (let page = 0; page < maxPages; page += 1) {
    const result = await transport.call(target, "getEntities", {
      parentId,
      offset,
      limit,
      filters: { withoutArchived: false, query: key },
    });
    if (!result || typeof result !== "object" || !Array.isArray(result.items))
      throw new ProtocolError("getEntities returned an unsupported result");
    const found = result.items.find(
      (item) => item?.type === "toggle" && item.path === entityPath
    );
    if (found) return found;
    if (!Number.isInteger(result.total) || result.total < 0)
      throw new ProtocolError("getEntities total is invalid");
    offset += result.items.length;
    if (offset >= result.total) return null;
    if (result.items.length === 0)
      throw new ProtocolError("getEntities pagination made no progress");
  }
  throw new ProtocolError("getEntities exceeded the pagination safety bound");
}

export function effectiveBoolean(toggle) {
  if (typeof toggle.isOn !== "boolean")
    throw new ProtocolError("toggle isOn is not boolean");
  const value = toggle.isOn ? toggle.onValue : toggle.offValue;
  if (typeof value !== "boolean")
    throw new ProtocolError("toggle effective value is not boolean");
  return value;
}
