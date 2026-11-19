import { assertIssueKey } from "./operations.mjs";
import { JiraProtocolError } from "./protocol.mjs";
import { getIssueResource } from "./extract.mjs";

// Read-only search for same-parent subtasks assigned to one exact login.
// The source issue itself is never reported as its own sibling. Every listed
// child is read individually by key because the parent subtask inventory does
// not guarantee a caller-visible assignee per child.
export function assertAssigneeLogin(value) {
  const login = String(value ?? "").trim();
  if (!/^[a-z0-9._@-]+$/i.test(login))
    throw new JiraProtocolError("assignee must be a Jira login");
  return login;
}

function incomplete(reason, details = {}) {
  return { status: "incomplete", reason, ...details };
}

function complete() {
  return { status: "complete" };
}

function categoryComplete(category) {
  return category?.status === "complete";
}

// Both supported assignee identities: the REST `name` and the legacy `key`.
// An absent assignee and an assignee without either identity are both
// reported as unassigned, never matched.
function assigneeLogins(assignee) {
  if (!assignee || typeof assignee !== "object" || Array.isArray(assignee))
    return [];
  return [assignee.name, assignee.key].filter(
    (value) => typeof value === "string" && value.length > 0
  );
}

function childEntry(payload, origin) {
  const fields =
    payload.fields && typeof payload.fields === "object" ? payload.fields : {};
  const status =
    fields.status &&
    typeof fields.status === "object" &&
    !Array.isArray(fields.status)
      ? fields.status
      : null;
  return {
    key: payload.key,
    summary: typeof fields.summary === "string" ? fields.summary : null,
    status: status && typeof status.name === "string" ? status.name : null,
    statusId:
      status && status.id !== undefined && status.id !== null
        ? String(status.id)
        : null,
    link: `${origin}/browse/${payload.key}`,
  };
}

function byKey(left, right) {
  return left.key < right.key ? -1 : left.key > right.key ? 1 : 0;
}

function finish(
  receipt,
  {
    parent,
    parentCategory,
    childListCategory,
    childReadsCategory,
    matches,
    unassigned,
    unreadable,
    examined,
  }
) {
  const categories = {
    parent: parentCategory,
    childList: childListCategory,
    childReads: childReadsCategory,
  };
  const comprehensive = Object.values(categories).every(categoryComplete);
  return {
    ...receipt,
    parent,
    matches: [...matches].sort(byKey),
    unassigned: [...unassigned].sort(),
    unreadable: [...unreadable].sort(byKey),
    examined,
    dataContract: { ...receipt.dataContract, comprehensive },
    completeness: {
      comprehensive,
      categories,
      gaps: Object.entries(categories)
        .filter(([, value]) => !categoryComplete(value))
        .map(([category, value]) => ({ category, ...value })),
    },
    provenance: {
      issueResource: "/rest/api/2/issue/{key}?fields=*all&expand=names,schema",
      childReads:
        "one issue-resource read per listed parent subtask, caller-visible fields only",
      linkBase: "{origin}/browse/{key}",
    },
  };
}

function unreadableResult(receipt, parent, reason, details = {}) {
  return finish(receipt, {
    parent,
    parentCategory: incomplete(reason, details),
    childListCategory: incomplete("parent_unreadable"),
    childReadsCategory: incomplete("parent_unreadable"),
    matches: [],
    unassigned: [],
    unreadable: [],
    examined: 0,
  });
}

export async function findSiblingIssues(
  transport,
  { issueKey, assignee } = {}
) {
  const sourceKey = assertIssueKey(issueKey);
  const login = assertAssigneeLogin(assignee);
  const receipt = {
    schema: "jira-protocol-result/2",
    operation: "siblings",
    dataContract: {
      kind: "sibling-search-by-exact-assignee",
      comprehensive: false,
      fullContentProven: false,
      scope:
        "one LEL source issue, its standard immediate parent, the parent listed subtasks, and one caller-visible issue-resource read per listed child",
    },
    source: { key: sourceKey },
    assignee: login,
  };

  // The source read stays terminal like the extractor: without it nothing
  // about the parent or the children can be proven.
  const source = await getIssueResource(transport, sourceKey);
  const fields = source.payload.fields;

  if (!Object.hasOwn(fields, "parent")) {
    return finish(receipt, {
      parent: { presence: "unobserved" },
      parentCategory: incomplete(
        "field_unobserved_permission_or_configuration"
      ),
      childListCategory: incomplete("parent_unobserved"),
      childReadsCategory: incomplete("parent_unobserved"),
      matches: [],
      unassigned: [],
      unreadable: [],
      examined: 0,
    });
  }
  if (fields.parent === null) {
    return finish(receipt, {
      parent: { presence: "absent" },
      parentCategory: complete(),
      childListCategory: complete(),
      childReadsCategory: complete(),
      matches: [],
      unassigned: [],
      unreadable: [],
      examined: 0,
    });
  }
  if (!fields.parent || typeof fields.parent !== "object") {
    return unreadableResult(
      receipt,
      { presence: "unobserved" },
      "malformed_parent_field"
    );
  }
  let parentKey;
  try {
    parentKey = assertIssueKey(fields.parent.key);
  } catch {
    return unreadableResult(
      receipt,
      { presence: "unobserved" },
      "invalid_or_out_of_scope_parent_key"
    );
  }
  if (parentKey === sourceKey) {
    return unreadableResult(
      receipt,
      { presence: "observed", key: parentKey },
      "parent_cycle"
    );
  }

  let parent;
  try {
    parent = await getIssueResource(transport, parentKey);
  } catch (error) {
    if (!(error instanceof JiraProtocolError)) throw error;
    if (error.status === 401 || error.status === 403)
      return unreadableResult(
        receipt,
        { presence: "observed", key: parentKey },
        "access_denied",
        {
          httpStatus: error.status,
        }
      );
    return unreadableResult(
      receipt,
      { presence: "observed", key: parentKey },
      error.status === 404 ? "not_found_or_not_visible" : "retrieval_unknown",
      ...(error.status ? [{ httpStatus: error.status }] : [{}])
    );
  }

  const subtasks = parent.payload.fields?.subtasks;
  if (subtasks === undefined) {
    return finish(receipt, {
      parent: { presence: "observed", key: parentKey },
      parentCategory: complete(),
      childListCategory: incomplete(
        "field_unobserved_permission_or_configuration"
      ),
      childReadsCategory: incomplete("child_list_unobserved"),
      matches: [],
      unassigned: [],
      unreadable: [],
      examined: 0,
    });
  }
  if (!Array.isArray(subtasks)) {
    return finish(receipt, {
      parent: { presence: "observed", key: parentKey },
      parentCategory: complete(),
      childListCategory: incomplete("malformed_child_list"),
      childReadsCategory: incomplete("child_list_unobserved"),
      matches: [],
      unassigned: [],
      unreadable: [],
      examined: 0,
    });
  }

  const childKeys = [];
  const invalidIndexes = [];
  for (const [index, child] of subtasks.entries()) {
    try {
      if (!child || typeof child !== "object") throw new Error("not an issue");
      childKeys.push(assertIssueKey(child.key));
    } catch {
      invalidIndexes.push(index);
    }
  }

  const matches = [];
  const unassigned = [];
  const unreadable = [];
  let examined = 0;
  const seen = new Set([sourceKey]);
  for (const childKey of childKeys) {
    if (seen.has(childKey)) continue;
    seen.add(childKey);
    examined += 1;
    let child;
    try {
      child = await getIssueResource(transport, childKey);
    } catch (error) {
      if (!(error instanceof JiraProtocolError)) throw error;
      if (error.status === 401 || error.status === 403)
        unreadable.push({
          key: childKey,
          reason: "access_denied",
          httpStatus: error.status,
        });
      else if (error.status === 404)
        unreadable.push({
          key: childKey,
          reason: "not_found_or_not_visible",
          httpStatus: 404,
        });
      else
        unreadable.push({
          key: childKey,
          reason: "retrieval_unknown",
          ...(error.status ? { httpStatus: error.status } : {}),
        });
      continue;
    }
    const logins = assigneeLogins(child.payload.fields?.assignee);
    if (logins.length === 0) {
      unassigned.push(childKey);
      continue;
    }
    if (logins.includes(login))
      matches.push(childEntry(child.payload, transport.origin));
  }

  return finish(receipt, {
    parent: { presence: "observed", key: parentKey },
    parentCategory: complete(),
    childListCategory:
      invalidIndexes.length === 0
        ? complete()
        : incomplete("malformed_child_entries", { invalidIndexes }),
    childReadsCategory:
      unreadable.length === 0
        ? complete()
        : incomplete("child_reads_incomplete", {
            keys: unreadable.map((entry) => entry.key),
          }),
    matches,
    unassigned,
    unreadable,
    examined,
  });
}
