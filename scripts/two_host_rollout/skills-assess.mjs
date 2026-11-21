#!/usr/bin/env node
// Shared skill-install recognition for issue #159. Read-only: lstat,
// readlink and file hashing only, never checkout/skill mutation. Both
// apply-host.mjs (executable rollout path) and diagnose-checkouts.mjs
// (read-only diagnosis) classify the same way so the operator sees the
// same basis before any change.
//
// Two managed models are standard and never overwritten blindly:
// - managed copy: a real directory carrying runtime-manifest.json with
//   schema cli-jira-skill-runtime/1. Current when the manifest matches the
//   assessed revision AND every listed payload file hashes to its manifest
//   entry (a changed copy refuses as foreign). Stale when the manifest is
//   schema-valid but made for another revision (proven owned, updatable
//   only through the compatible reinstall route after ownership is
//   re-proven against the pre-mutation revision).
// - managed junction: a link (Windows junction or symlink) resolving
//   exactly to <checkout>/skills/<name> with SKILL.md present. It follows
//   the checkout across revisions, so it needs no reinstall; a link
//   anywhere else (another checkout, PAT sources, broken target) is
//   foreign and refuses before any overwrite.
// Anything else (missing entry, file, dir without a managed manifest,
// changed copy, mixed junction+copy models in one dir) maps to missing,
// partial-install or ownership-conflict, never to silent success.
import { createHash } from "node:crypto";
import { existsSync, lstatSync, readFileSync, readlinkSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

export const MANIFEST_SCHEMA = "cli-jira-skill-runtime/1";

// Same runtime->manifest transform the copy installer writes:
// docs/jira-protocol*.md payloads land under references/ in the install.
export function buildExpectedManifest(fresh) {
  const expected = { ...fresh };
  expected["references/jira-protocol.md"] = fresh["docs/jira-protocol.md"];
  expected["references/jira-protocol-subtasks.md"] =
    fresh["docs/jira-protocol-subtasks.md"];
  expected["references/jira-protocol-batch.md"] =
    fresh["docs/jira-protocol-batch.md"];
  expected["references/jira-protocol-handoff.md"] =
    fresh["docs/jira-protocol-handoff.md"];
  expected["references/jira-protocol-read-fields.md"] =
    fresh["docs/jira-protocol-read-fields.md"];
  delete expected["docs/jira-protocol.md"];
  delete expected["docs/jira-protocol-subtasks.md"];
  delete expected["docs/jira-protocol-batch.md"];
  delete expected["docs/jira-protocol-handoff.md"];
  delete expected["docs/jira-protocol-read-fields.md"];
  return expected;
}

// Manifest keys double as install-relative paths (references/* included).
export function installedPathForKey(dir, key) {
  return join(dir, key);
}

// Resolved link target or null when the entry is not a link (plain dir,
// file, missing). Works for Windows junctions and symlinks alike.
export function linkTargetOf(entryPath) {
  let raw;
  try {
    raw = readlinkSync(entryPath);
  } catch {
    return null;
  }
  if (!raw) return null;
  return resolve(dirname(entryPath), String(raw));
}

function samePath(a, b) {
  if (process.platform === "win32") return a.toLowerCase() === b.toLowerCase();
  return a === b;
}

// A junction is managed only when it points exactly into the rollout
// checkout being assessed and the skill source is present there. Links
// into any other checkout stay foreign: ownership is ambiguous there.
export function assessJunction({ entryPath, checkout, name }) {
  const target = linkTargetOf(entryPath);
  if (!target) return { isLink: false, target: null };
  const expected = resolve(join(checkout, "skills", name));
  if (!samePath(target, expected)) {
    return {
      isLink: true,
      ok: false,
      target,
      detail: `foreign link -> ${target}`,
    };
  }
  if (!existsSync(join(expected, "SKILL.md"))) {
    return {
      isLink: true,
      ok: false,
      target,
      detail: "junction target has no SKILL.md",
    };
  }
  return { isLink: true, ok: true, target };
}

function hashFile(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

// Per-skill verdict. state is one of: missing, junction-current,
// copy-current, copy-stale, foreign. Only the first three carry a proven
// model; foreign always names its basis in detail.
export function assessSkillEntry({ skillsDir, name, checkout, fresh }) {
  const dir = join(skillsDir, name);
  let st;
  try {
    st = lstatSync(dir);
  } catch (error) {
    if (error?.code === "ENOENT") return { name, kind: "missing", state: "missing", linkTarget: null, detail: "not installed" };
    return { name, kind: "unknown", state: "foreign", linkTarget: null, detail: "unreadable entry" };
  }
  const junction = assessJunction({ entryPath: dir, checkout, name });
  if (junction.isLink) {
    if (junction.ok) {
      return { name, kind: "link", state: "junction-current", model: "junction", linkTarget: junction.target, detail: `managed junction -> ${join(checkout, "skills", name)}` };
    }
    return { name, kind: "link", state: "foreign", linkTarget: junction.target, detail: junction.detail };
  }
  if (!st.isDirectory()) {
    return { name, kind: "file", state: "foreign", linkTarget: null, detail: "not a directory or managed link" };
  }
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(join(dir, "runtime-manifest.json"), "utf8"));
  } catch {
    return { name, kind: "dir", state: "foreign", linkTarget: null, detail: "no managed manifest" };
  }
  if (manifest?.schema !== MANIFEST_SCHEMA || typeof manifest.files !== "object" || manifest.files === null) {
    return { name, kind: "dir", state: "foreign", linkTarget: null, detail: "no managed manifest" };
  }
  for (const [key, recorded] of Object.entries(manifest.files)) {
    let actual;
    try {
      actual = hashFile(installedPathForKey(dir, key));
    } catch {
      return { name, kind: "dir", state: "foreign", linkTarget: null, detail: `changed copy (unreadable ${key})` };
    }
    if (actual !== recorded) {
      return { name, kind: "dir", state: "foreign", linkTarget: null, detail: `changed copy (content differs: ${key})` };
    }
  }
  const expected = buildExpectedManifest(fresh);
  const keys = Object.keys(expected);
  if (keys.length > 0 && keys.every((k) => manifest.files?.[k] === expected[k]) && Object.keys(manifest.files).length === keys.length) {
    return { name, kind: "dir", state: "copy-current", model: "copy", linkTarget: null, detail: "managed copy, manifest current" };
  }
  return { name, kind: "dir", state: "copy-stale", model: "copy", linkTarget: null, manifest: manifest.files, detail: "managed copy, stale for this revision" };
}

// Overall verdict over one skills dir. state is one of: missing,
// current, stale-managed, partial-install, ownership-conflict.
// Uniform model is required for current/stale-managed: a mix of proven
// junctions and copies in one dir is ambiguous and refuses.
export function assessAllSkills({ skillsDir, checkout, fresh, names }) {
  const perSkill = names.map((name) => assessSkillEntry({ skillsDir, name, checkout, fresh }));
  const states = new Set(perSkill.map((s) => s.state));
  if (perSkill.every((s) => s.state === "missing")) {
    return { state: "missing", names, fresh, perSkill, model: null, detail: "no skills installed" };
  }
  const foreign = perSkill.find((s) => s.state === "foreign");
  if (foreign) {
    return { state: "ownership-conflict", names, fresh, perSkill, model: null, detail: `${foreign.name}: ${foreign.detail}` };
  }
  const models = new Set(perSkill.filter((s) => s.model).map((s) => s.model));
  if (models.size > 1) {
    return { state: "ownership-conflict", names, fresh, perSkill, model: null, detail: "mixed install models (junction + copy); clear one model manually" };
  }
  const model = models.size === 1 ? [...models][0] : null;
  if (perSkill.some((s) => s.state === "missing")) {
    return { state: "partial-install", names, fresh, perSkill, model, detail: "some skills installed, some missing" };
  }
  if (perSkill.every((s) => s.state === "junction-current" || s.state === "copy-current")) {
    return { state: "current", names, fresh, perSkill, model, detail: "all skills current" };
  }
  return { state: "stale-managed", names, fresh, perSkill, model, detail: "managed install, stale for this revision" };
}
