import {
  JiraAmbiguousOutcome,
  JiraProtocolError,
  confirmationDigest,
} from "./protocol.mjs";
import {
  CONTRACT,
  assertIssueKey,
  boardData,
  getIssueState,
  getIssueTransitions,
  integer,
} from "./operations-shared.mjs";

export async function planTransition(
  transport,
  { issueKey, transitionId, targetColumn }
) {
  const key = assertIssueKey(issueKey);
  const selectedTransitionId = integer(transitionId, "transition-id");
  const selectedTargetColumn = integer(targetColumn, "target-column");
  const [{ issue, transitions }, board] = await Promise.all([
    getIssueTransitions(transport, key),
    boardData(transport),
  ]);
  const transition = transitions.find(
    (item) => Number(item?.id) === selectedTransitionId
  );
  if (!transition)
    throw new JiraProtocolError("transition is not currently available");
  if (transition.hasTransitionView !== false)
    throw new JiraProtocolError(
      "transition requires a workflow form and is unsupported by this CLI"
    );
  const column = board.columnsData.columns.find(
    (item) => Number(item?.id) === selectedTargetColumn
  );
  if (!column)
    throw new JiraProtocolError(
      `target column is not on board ${CONTRACT.rapidViewId}`
    );
  if (!column.statusIds.map(String).includes(String(transition.targetStatus)))
    throw new JiraProtocolError(
      "target column does not contain the transition target status"
    );
  const operation = {
    kind: "transition",
    issueKey: key,
    transitionId: selectedTransitionId,
    transitionName: String(transition.name ?? ""),
    targetColumn: selectedTargetColumn,
    targetColumnName: String(column.name ?? ""),
    targetStatus: String(transition.targetStatus),
  };
  return {
    operation,
    confirmation: confirmationDigest(operation),
    issue: {
      key,
      id: Number(issue.id),
      isSubtask: Boolean(issue.isSubtask),
    },
    transition: {
      id: selectedTransitionId,
      name: transition.name,
      targetStatus: String(transition.targetStatus),
    },
    column: { id: selectedTargetColumn, name: column.name },
  };
}

export async function transitionIssue(transport, input) {
  const plan = await planTransition(transport, input);
  if (input.confirm !== plan.confirmation)
    return { state: "AwaitingConfirmation", ...plan };
  await transport.request({
    method: "PUT",
    path: "/rest/greenhopper/1.0/xboard/transitionAndRank",
    json: {
      rapidViewId: CONTRACT.rapidViewId,
      issueKeys: [plan.issue.key],
      targetColumn: plan.column.id,
      selectedTransitionId: plan.transition.id,
    },
    mutation: true,
  });
  let readback;
  try {
    readback = await getIssueState(transport, plan.issue.key);
  } catch (error) {
    throw new JiraAmbiguousOutcome("transition readback failed", {
      cause: error,
    });
  }
  if (String(readback.fields.status?.id) !== plan.transition.targetStatus)
    throw new JiraAmbiguousOutcome(
      "transition response could not be confirmed by issue readback"
    );
  return {
    state: "Completed",
    ...plan,
    readback: { statusId: String(readback.fields.status.id) },
  };
}
