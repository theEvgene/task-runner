import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import {
  CONTRACT,
  assertIssueKey,
  getIssue,
  getIssueState,
} from "./operations-shared.mjs";

export async function assignIssue(transport, { issueKey, assignee, confirm }) {
  const key = assertIssueKey(issueKey);
  const login = String(assignee ?? "").trim();
  if (!/^[a-z0-9._@-]+$/i.test(login))
    throw new JiraProtocolError("assignee must be a Jira login");
  const issue = await getIssue(transport, key);
  const operation = { kind: "assign", issueKey: key, assignee: login };
  const confirmation = confirmationDigest(operation);
  const plan = {
    operation,
    confirmation,
    issue: { key, id: Number(issue.id), isSubtask: Boolean(issue.isSubtask) },
  };
  if (confirm !== confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  if (!transport.atlToken)
    throw new JiraProtocolError("JIRA_ATL_TOKEN is required for assignment");
  await transport.request({
    path: "/secure/AssignIssue.jspa",
    query: {
      atl_token: transport.atlToken,
      id: issue.id,
      assignee: login,
    },
    expect: "text",
    mutation: true,
  });
  let readback;
  try {
    readback = await getIssueState(transport, key);
  } catch (error) {
    throw new JiraAmbiguousOutcome("assignment readback failed", {
      cause: error,
    });
  }
  const readbackLogin =
    readback.fields.assignee?.name ?? readback.fields.assignee?.key;
  if (readbackLogin !== login)
    throw new JiraAmbiguousOutcome(
      "assignment response could not be confirmed by issue readback"
    );
  return {
    state: "Completed",
    ...plan,
    readback: { assignee: readbackLogin },
  };
}
