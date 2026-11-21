import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import { defaultsFromEditHtml } from "./html-form.mjs";
import {
  assertIssueKey,
  comments,
  getIssueState,
} from "./operations-shared.mjs";

export async function flagIssue(
  transport,
  { issueKey, action, comment, confirm }
) {
  const key = assertIssueKey(issueKey);
  if (!["add", "remove"].includes(action))
    throw new JiraProtocolError("action must be add or remove");
  const text = String(comment ?? "").trim();
  if (!text) throw new JiraProtocolError("comment is required");
  const flag = action === "add";
  const operation = { kind: "flag", issueKey: key, action, comment: text };
  const confirmation = confirmationDigest(operation);
  const plan = { operation, confirmation };
  if (confirm !== confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  const state = (value) => {
    if (!(value === null || Array.isArray(value)))
      throw new JiraProtocolError("unsupported flag readback");
    return Array.isArray(value) && value.length > 0;
  };
  const before = await getIssueState(transport, key, ["customfield_10641"]);
  if (state(before.fields.customfield_10641) === flag)
    throw new JiraProtocolError(
      "issue already has requested flag state; no comment posted"
    );
  const oldIds = new Set(
    (await comments(transport, key)).map((c) => String(c.id))
  );
  const { payload: html } = await transport.request({
    path: "/secure/GHBulkCommentDialog.jspa",
    query: { issueKeys: key },
    expect: "text",
  });
  if (typeof html !== "string")
    throw new JiraProtocolError("unsupported flag dialog");
  const tokens = defaultsFromEditHtml(html).get("formToken") ?? [];
  if (tokens.length !== 1 || !tokens[0])
    throw new JiraProtocolError("flag dialog has no unique formToken");
  const escaped = text.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ]
  );
  const prefix = flag ? "(flag) Flag added" : "(flagoff) Flag removed";
  const body = `${prefix}\n\n${escaped}`;
  let writePayload;
  let writeUnreadable = false;
  try {
    ({ payload: writePayload } = await transport.request({
      method: "POST",
      path: "/rest/greenhopper/1.0/xboard/issue/flag/flag.json",
      json: {
        issueKeys: [key],
        flag,
        comment: body,
        commentVisibility: "",
        formToken: tokens[0],
        temporaryAttachments: [],
      },
      mutation: true,
    }));
  } catch (error) {
    // Reproducible boundary (no live server cause evidenced): a 2xx flag
    // write with an empty or non-JSON body surfaces here as an unreadable
    // success from JiraTransport. The write may have applied, so perform
    // exactly one safe flag-only readback below; never retry the write.
    if (
      error instanceof JiraAmbiguousOutcome &&
      /unreadable success/.test(error.message)
    ) {
      writeUnreadable = true;
    } else {
      throw error;
    }
  }
  if (!writeUnreadable) {
    if (
      Array.isArray(writePayload?.errorMessages) &&
      writePayload.errorMessages.length
    )
      throw new JiraAmbiguousOutcome(
        "flag response reported errors; verify before retrying"
      );
  }
  try {
    const issue = await getIssueState(transport, key, ["customfield_10641"]);
    const after = await comments(transport, key);
    const found = after.filter(
      (c) =>
        !oldIds.has(String(c.id)) &&
        (c.body === body || c.body === `${prefix}\n\n${text}`)
    );
    if (state(issue.fields.customfield_10641) !== flag || found.length !== 1)
      throw new Error("readback mismatch");
    return {
      state: "Completed",
      ...plan,
      readback: { flag, commentId: String(found[0].id) },
    };
  } catch (error) {
    throw new JiraAmbiguousOutcome(
      "flag and new comment could not both be confirmed; verify before retrying",
      { cause: error }
    );
  }
}
