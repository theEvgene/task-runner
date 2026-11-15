import { assertIssueKey } from "./operations.mjs";
import { JiraProtocolError } from "./protocol.mjs";

const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_MAX_PAGES = 100;
const DEFAULT_MAX_ITEMS = 10_000;

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function positiveInteger(value, name, fallback) {
  if (value === undefined) return fallback;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0)
    throw new JiraProtocolError(`${name} must be a positive integer`);
  return parsed;
}

function pageInteger(value, name, { positive = false } = {}) {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    (positive && value === 0)
  )
    throw new JiraProtocolError(
      `${name} must be ${positive ? "a positive" : "a non-negative"} integer`
    );
  return value;
}

function genericIssueKey(value) {
  const key = String(value ?? "")
    .trim()
    .toUpperCase();
  if (!/^[A-Z][A-Z0-9_]*-\d+$/.test(key))
    throw new JiraProtocolError("linked issue key is invalid");
  return key;
}

function validateIssueSelf(payload, expectedOrigin, key) {
  if (payload.self === undefined) return "unobserved";
  let self;
  try {
    self = new URL(payload.self);
  } catch {
    throw new JiraProtocolError("issue resource self URL is invalid");
  }
  if (self.origin !== expectedOrigin)
    throw new JiraProtocolError(
      "issue resource came from another Jira instance"
    );
  const tail = decodeURIComponent(
    self.pathname.split("/").filter(Boolean).at(-1)
  );
  const allowed = new Set([key, String(payload.id ?? "")]);
  if (!allowed.has(tail))
    throw new JiraProtocolError(
      "issue resource self URL did not match the issue"
    );
  return "validated";
}

export async function getIssueResource(transport, issueKey) {
  const key = assertIssueKey(issueKey);
  const { payload } = await transport.request({
    path: `/rest/api/2/issue/${key}`,
    query: { fields: "*all", expand: "names,schema" },
  });
  if (!object(payload) || payload.key !== key || !object(payload.fields))
    throw new JiraProtocolError(
      "issue resource did not match the requested key and fields contract"
    );
  const instance = validateIssueSelf(payload, transport.origin, key);
  return { payload, instance };
}

function incomplete(reason, details = {}) {
  return { status: "incomplete", reason, ...details };
}

export async function collectComments(
  transport,
  issueKey,
  { pageSize, maxPages, maxItems } = {}
) {
  const key = assertIssueKey(issueKey);
  const limit = positiveInteger(pageSize, "page-size", DEFAULT_PAGE_SIZE);
  const pageLimit = positiveInteger(maxPages, "max-pages", DEFAULT_MAX_PAGES);
  const itemLimit = positiveInteger(maxItems, "max-items", DEFAULT_MAX_ITEMS);
  const comments = [];
  const ids = new Set();
  const pages = [];
  let expectedTotal;
  let startAt = 0;

  while (expectedTotal === undefined || comments.length < expectedTotal) {
    if (pages.length >= pageLimit)
      return {
        comments,
        pagination: { pages, expectedTotal, collected: comments.length },
        completeness: incomplete("max_pages_exceeded", {
          maxPages: pageLimit,
        }),
      };
    const { payload } = await transport.request({
      path: `/rest/api/2/issue/${key}/comment`,
      query: { startAt, maxResults: limit },
    });
    if (!object(payload) || !Array.isArray(payload.comments))
      throw new JiraProtocolError(
        "comments page did not match the Jira collection contract"
      );
    const actualStart = pageInteger(payload.startAt, "comments startAt");
    const serverMax = pageInteger(payload.maxResults, "comments maxResults", {
      positive: true,
    });
    const total = pageInteger(payload.total, "comments total");
    if (
      actualStart > total ||
      payload.comments.length > serverMax ||
      actualStart + payload.comments.length > total
    )
      throw new JiraProtocolError(
        "comments page pagination metadata is internally inconsistent"
      );
    if (actualStart !== startAt)
      return {
        comments,
        pagination: { pages, expectedTotal, collected: comments.length },
        completeness: incomplete("unexpected_start_at", {
          requested: startAt,
          returned: actualStart,
        }),
      };
    if (expectedTotal === undefined) expectedTotal = total;
    else if (total !== expectedTotal)
      return {
        comments,
        pagination: { pages, expectedTotal, collected: comments.length },
        completeness: incomplete("total_changed", {
          previous: expectedTotal,
          returned: total,
        }),
      };
    if (total > itemLimit)
      return {
        comments,
        pagination: { pages, expectedTotal, collected: comments.length },
        completeness: incomplete("max_items_exceeded", { maxItems: itemLimit }),
      };
    pages.push({
      startAt: actualStart,
      maxResults: serverMax,
      returned: payload.comments.length,
      total,
    });
    if (payload.comments.length === 0 && comments.length < total)
      return {
        comments,
        pagination: { pages, expectedTotal, collected: comments.length },
        completeness: incomplete("empty_page_before_total"),
      };
    for (const comment of payload.comments) {
      if (!object(comment) || !/^\d+$/.test(String(comment.id ?? "")))
        return {
          comments,
          pagination: { pages, expectedTotal, collected: comments.length },
          completeness: incomplete("invalid_comment_identity"),
        };
      const id = String(comment.id);
      if (ids.has(id))
        return {
          comments,
          pagination: { pages, expectedTotal, collected: comments.length },
          completeness: incomplete("duplicate_comment_id", { id }),
        };
      ids.add(id);
      comments.push(comment);
    }
    if (comments.length > total)
      return {
        comments,
        pagination: { pages, expectedTotal, collected: comments.length },
        completeness: incomplete("rows_exceed_total"),
      };
    startAt += payload.comments.length;
  }

  return {
    comments,
    pagination: { pages, expectedTotal, collected: comments.length },
    completeness: { status: "complete" },
  };
}

function sanitizeUrl(value, origin) {
  if (value === undefined || value === null) return null;
  let url;
  try {
    url = new URL(String(value), origin);
  } catch {
    return { status: "invalid", retrieval: "not_attempted" };
  }
  if (url.username || url.password)
    return { status: "credential_bearing", retrieval: "not_attempted" };
  for (const name of [...url.searchParams.keys()]) {
    if (/(?:token|auth|cookie|session)/i.test(name))
      url.searchParams.set(name, "[REDACTED]");
  }
  return {
    status: url.origin === origin ? "same_origin" : "cross_origin_unverified",
    url: url.href,
    retrieval: "not_attempted",
  };
}

function attachmentInventory(fields, origin) {
  if (!Object.hasOwn(fields, "attachment"))
    return {
      items: [],
      completeness: incomplete("field_unobserved_permission_or_configuration"),
    };
  if (!Array.isArray(fields.attachment))
    return {
      items: [],
      completeness: incomplete("malformed_attachment_field"),
    };
  const items = [];
  const invalid = [];
  for (const [index, attachment] of fields.attachment.entries()) {
    if (
      !object(attachment) ||
      !/^\d+$/.test(String(attachment.id ?? "")) ||
      typeof attachment.filename !== "string"
    ) {
      invalid.push(index);
      continue;
    }
    items.push({
      id: String(attachment.id),
      filename: attachment.filename,
      author: attachment.author ?? null,
      created: attachment.created ?? null,
      size: attachment.size ?? null,
      mimeType: attachment.mimeType ?? null,
      content: sanitizeUrl(attachment.content, origin),
      thumbnail: sanitizeUrl(attachment.thumbnail, origin),
    });
  }
  return {
    items,
    completeness:
      invalid.length === 0
        ? { status: "complete" }
        : incomplete("malformed_attachment_entries", {
            invalidIndexes: invalid,
          }),
  };
}

function linkedIssueSummary(issue) {
  const key = genericIssueKey(issue?.key);
  return {
    key,
    id: issue.id === undefined ? null : String(issue.id),
    self: issue.self ?? null,
    fields: object(issue.fields)
      ? {
          summary: issue.fields.summary ?? null,
          status: issue.fields.status ?? null,
          issuetype: issue.fields.issuetype ?? null,
        }
      : null,
  };
}

function issueLinkInventory(fields) {
  if (!Object.hasOwn(fields, "issuelinks"))
    return {
      items: [],
      completeness: incomplete("field_unobserved_permission_or_configuration"),
    };
  if (!Array.isArray(fields.issuelinks))
    return {
      items: [],
      completeness: incomplete("malformed_issue_links_field"),
    };
  const items = [];
  const invalid = [];
  for (const [index, link] of fields.issuelinks.entries()) {
    try {
      if (!object(link) || !object(link.type))
        throw new JiraProtocolError("invalid issue link");
      const inward = object(link.inwardIssue);
      const outward = object(link.outwardIssue);
      if (inward === outward) throw new JiraProtocolError("invalid issue link");
      items.push({
        id: link.id === undefined ? null : String(link.id),
        type: {
          id: link.type.id === undefined ? null : String(link.type.id),
          name: link.type.name ?? null,
          inward: link.type.inward ?? null,
          outward: link.type.outward ?? null,
        },
        direction: inward ? "inward" : "outward",
        issue: linkedIssueSummary(
          inward ? link.inwardIssue : link.outwardIssue
        ),
      });
    } catch (error) {
      if (!(error instanceof JiraProtocolError)) throw error;
      invalid.push(index);
    }
  }
  return {
    items,
    completeness:
      invalid.length === 0
        ? { status: "complete" }
        : incomplete("malformed_issue_link_entries", {
            invalidIndexes: invalid,
          }),
  };
}

function parentInventory(fields) {
  if (!Object.hasOwn(fields, "parent"))
    return {
      presence: "unobserved",
      completeness: incomplete("field_unobserved_permission_or_configuration"),
    };
  if (fields.parent === null)
    return { presence: "absent", completeness: { status: "complete" } };
  if (!object(fields.parent))
    return {
      presence: "unobserved",
      completeness: incomplete("malformed_parent_field"),
    };
  try {
    return {
      presence: "observed",
      key: assertIssueKey(fields.parent.key),
      summary: object(fields.parent.fields)
        ? (fields.parent.fields.summary ?? null)
        : null,
      completeness: { status: "pending_expansion" },
    };
  } catch {
    return {
      presence: "unobserved",
      completeness: incomplete("invalid_or_out_of_scope_parent_key"),
    };
  }
}

function categoryComplete(category) {
  return category?.status === "complete";
}

function permissionAwareFailure(error, category) {
  if (!(error instanceof JiraProtocolError)) throw error;
  if (error.status === 401 || error.status === 403) throw error;
  return incomplete(
    error.status === 404 ? "not_found_or_not_visible" : "retrieval_unknown",
    { category, ...(error.status ? { httpStatus: error.status } : {}) }
  );
}

async function extractOne(transport, issueKey, options) {
  const key = assertIssueKey(issueKey);
  const first = await getIssueResource(transport, key);
  let comments;
  try {
    comments = await collectComments(transport, key, options);
  } catch (error) {
    comments = {
      comments: [],
      pagination: { pages: [], collected: 0 },
      completeness: permissionAwareFailure(error, "comments"),
    };
  }
  const attachments = attachmentInventory(
    first.payload.fields,
    transport.origin
  );
  const issueLinks = issueLinkInventory(first.payload.fields);
  const parent = parentInventory(first.payload.fields);
  const final = await getIssueResource(transport, key);
  const firstUpdated = first.payload.fields.updated;
  const finalUpdated = final.payload.fields.updated;
  const snapshot =
    firstUpdated === undefined || finalUpdated === undefined
      ? incomplete("updated_timestamp_unobserved")
      : firstUpdated === finalUpdated
        ? { status: "complete" }
        : incomplete("concurrently_modified", {
            before: firstUpdated,
            after: finalUpdated,
          });
  const categories = {
    coreFields: {
      status: "complete",
      visibility: "caller_visible_fields_only",
      instanceSelf: first.instance,
    },
    comments: comments.completeness,
    attachments: attachments.completeness,
    issueLinks: issueLinks.completeness,
    snapshot,
  };
  const comprehensive = Object.values(categories).every(categoryComplete);
  return {
    key,
    resource: first.payload,
    comments: comments.comments,
    attachments: attachments.items,
    issueLinks: issueLinks.items,
    parentObservation: parent,
    pagination: { comments: comments.pagination },
    completeness: {
      scope:
        "caller-visible Jira REST fields, dedicated comments, attachment metadata, and Jira issue links",
      comprehensive,
      categories,
    },
  };
}

function extendedGaps() {
  return {
    serverRenderedContent: {
      status: "unproven_not_retrieved",
      reason:
        "GreenHopper/server-rendered UI sections are outside the REST extraction contract",
    },
    wiki: {
      status: "unproven_not_retrieved",
      reason:
        "linked Wiki pages require a separate authenticated system and were not contacted",
    },
    attachmentContent: {
      status: "metadata_only_not_retrieved",
      reason:
        "attachment bodies and accessible text require a separate bounded download contract",
    },
    remoteLinks: {
      status: "unproven_not_retrieved",
      reason:
        "the Jira remotelink endpoint contract is not pinned for this instance",
    },
  };
}

export async function extractIssue(transport, issueKey, options = {}) {
  const key = assertIssueKey(issueKey);
  const issue = await extractOne(transport, key, options);
  let parent;
  let parentCategory = issue.parentObservation.completeness;
  if (issue.parentObservation.presence === "observed") {
    if (issue.parentObservation.key === key) {
      parent = { presence: "observed", key, retrieval: "refused_cycle" };
      parentCategory = incomplete("parent_cycle");
    } else {
      try {
        const extracted = await extractOne(
          transport,
          issue.parentObservation.key,
          options
        );
        parent = {
          presence: "observed",
          key: issue.parentObservation.key,
          issue: extracted,
          furtherParent: extracted.parentObservation,
        };
        parentCategory = extracted.completeness.comprehensive
          ? { status: "complete" }
          : incomplete("parent_content_incomplete");
      } catch (error) {
        parent = {
          presence: "observed",
          key: issue.parentObservation.key,
          retrieval: "failed",
        };
        parentCategory = permissionAwareFailure(error, "parent");
      }
    }
  } else {
    parent = { presence: issue.parentObservation.presence };
  }

  const categories = {
    ...issue.completeness.categories,
    parent: parentCategory,
  };
  const comprehensive = Object.values(categories).every(categoryComplete);
  return {
    schema: "jira-protocol-result/2",
    operation: "extract",
    dataContract: {
      kind: "jira-rest-visible-metadata",
      comprehensive,
      fullContentProven: false,
      scope:
        "one LEL issue plus its standard immediate parent, caller-visible REST fields, all dedicated comments within safety bounds, attachment metadata, and Jira issue links",
    },
    issue,
    parent,
    completeness: {
      comprehensive,
      categories,
      extendedContent: extendedGaps(),
      gaps: [
        ...Object.entries(categories)
          .filter(([, value]) => !categoryComplete(value))
          .map(([category, value]) => ({ category, ...value })),
        ...Object.entries(extendedGaps()).map(([category, value]) => ({
          category,
          ...value,
        })),
      ],
    },
    provenance: {
      issueResource: "/rest/api/2/issue/{key}?fields=*all&expand=names,schema",
      comments: "/rest/api/2/issue/{key}/comment?startAt={n}&maxResults={n}",
      parentDepth: 1,
      attachmentBodiesFetched: false,
      wikiFetched: false,
      remoteLinksFetched: false,
    },
  };
}
