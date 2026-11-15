import { canonicalJson } from "./model.mjs";

export const STATE_SCHEMA = "dco-feature-flag-execution/2";

const STATES = new Set([
  "New",
  "AwaitingConfirmation",
  "ReadyToCreate",
  "UnknownOutcome",
  "AwaitingRenewedConfirmation",
  "TestVerified",
  "Completed",
  "Duplicate",
  "FailedTest",
  "Partial",
]);

export const TERMINAL_STATES = new Set([
  "Completed",
  "Duplicate",
  "FailedTest",
  "Partial",
]);

const MUTATIONS = new Set([
  "pending",
  "started",
  "verified",
  "failed",
  "absent_after_unknown",
  "exact_path_absent",
  "unexpected_value",
  "readback_unknown",
]);

function exactKeys(value, expected, label) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    canonicalJson(Object.keys(value).sort()) !==
      canonicalJson([...expected].sort())
  )
    throw new Error(`${label} has an unsupported shape`);
}

function validateRow(row, target) {
  exactKeys(
    row,
    [
      "preflight",
      "mutation",
      "observedValue",
      "parentPath",
      "exactPath",
      "valueTypeId",
      "toggleId",
    ],
    `${target.environment} execution target`
  );
  if (!["pending", "clear", "duplicate"].includes(row.preflight))
    throw new Error(`${target.environment} preflight is invalid`);
  if (!MUTATIONS.has(row.mutation))
    throw new Error(`${target.environment} mutation is invalid`);
  if (![null, true, false].includes(row.observedValue))
    throw new Error(`${target.environment} observed value is invalid`);
  if (row.exactPath !== target.fullPath)
    throw new Error(`${target.environment} exact path is inconsistent`);
  for (const name of ["parentPath", "valueTypeId", "toggleId"])
    if (row[name] !== null && (typeof row[name] !== "string" || !row[name]))
      throw new Error(`${target.environment} ${name} is invalid`);
  if (row.preflight === "pending") {
    if (
      row.parentPath !== null ||
      row.valueTypeId !== null ||
      row.toggleId !== null ||
      row.mutation !== "pending" ||
      row.observedValue !== null
    )
      throw new Error(
        `${target.environment} pending preflight is inconsistent`
      );
  } else if (row.parentPath === null || row.valueTypeId === null) {
    throw new Error(`${target.environment} preflight data is incomplete`);
  }
  if (row.preflight === "duplicate" && row.mutation !== "pending")
    throw new Error(`${target.environment} duplicate target was mutated`);
  if (
    row.mutation === "verified" &&
    (row.observedValue !== target.expectedValue || row.toggleId === null)
  )
    throw new Error(`${target.environment} verified row is inconsistent`);
  if (row.mutation === "started" && row.observedValue !== null)
    throw new Error(`${target.environment} started mutation is inconsistent`);
}

export function validateState(state, plan) {
  exactKeys(
    state,
    [
      "schema",
      "planDigest",
      "state",
      "confirmedDigest",
      "unknownEnvironment",
      "targets",
    ],
    "execution state"
  );
  if (state.schema !== STATE_SCHEMA || state.planDigest !== plan.planDigest)
    throw new Error("execution state is not bound to this plan");
  if (!STATES.has(state.state)) throw new Error("execution state is unknown");
  if (![null, plan.planDigest].includes(state.confirmedDigest))
    throw new Error("execution confirmation is inconsistent");
  if (![null, "test", "production"].includes(state.unknownEnvironment))
    throw new Error("execution unknown target is invalid");
  exactKeys(state.targets, ["test", "production"], "execution targets");
  for (const target of plan.targets)
    validateRow(state.targets[target.environment], target);

  const test = state.targets.test;
  const production = state.targets.production;
  const mappingSupported = plan.targets.every(
    (target) =>
      typeof target.createPath === "string" &&
      target.createPath.length > 0 &&
      ["observed-create-readback", "inferred-from-stage-contract"].includes(
        target.protocolEvidence
      )
  );
  if (state.state === "New") {
    if (
      state.confirmedDigest !== null ||
      state.unknownEnvironment !== null ||
      ![test, production].every((row) => row.preflight === "pending")
    )
      throw new Error("new execution state is inconsistent");
    return state;
  }
  if (state.state === "Duplicate") {
    if (
      state.confirmedDigest !== null ||
      state.unknownEnvironment !== null ||
      ![test, production].some((row) => row.preflight === "duplicate") ||
      ![test, production].every((row) => row.mutation === "pending")
    )
      throw new Error("duplicate execution state is inconsistent");
    return state;
  }
  if (
    [
      "AwaitingConfirmation",
      "ReadyToCreate",
      "UnknownOutcome",
      "AwaitingRenewedConfirmation",
      "TestVerified",
      "Completed",
    ].includes(state.state) &&
    !mappingSupported
  )
    throw new Error(
      `${state.state} requires a supported create/readback contract`
    );
  if (![test, production].every((row) => row.preflight === "clear"))
    throw new Error(`${state.state} requires two clear preflights`);
  if (state.state === "AwaitingConfirmation") {
    if (
      state.confirmedDigest !== null ||
      state.unknownEnvironment !== null ||
      ![test, production].every((row) => row.mutation === "pending")
    )
      throw new Error("confirmation state is inconsistent");
  } else if (state.state === "ReadyToCreate") {
    if (
      state.confirmedDigest !== plan.planDigest ||
      state.unknownEnvironment !== null ||
      !mappingSupported ||
      !(
        (test.mutation === "pending" && production.mutation === "pending") ||
        (test.mutation === "verified" && production.mutation === "pending")
      )
    )
      throw new Error("create authorization is inconsistent");
  } else if (state.state === "UnknownOutcome") {
    const row = state.targets[state.unknownEnvironment];
    if (
      state.confirmedDigest !== plan.planDigest ||
      !row ||
      row.mutation !== "started" ||
      (state.unknownEnvironment === "production" &&
        test.mutation !== "verified")
    )
      throw new Error("unknown outcome state is inconsistent");
  } else if (state.state === "AwaitingRenewedConfirmation") {
    const row = state.targets[state.unknownEnvironment];
    if (
      state.confirmedDigest !== null ||
      !row ||
      row.mutation !== "absent_after_unknown" ||
      (state.unknownEnvironment === "production" &&
        test.mutation !== "verified")
    )
      throw new Error("renewed confirmation state is inconsistent");
  } else if (state.state === "TestVerified") {
    if (
      state.confirmedDigest !== plan.planDigest ||
      state.unknownEnvironment !== null ||
      test.mutation !== "verified" ||
      production.mutation !== "pending"
    )
      throw new Error("test verification state is inconsistent");
  } else if (state.state === "Completed") {
    if (
      state.confirmedDigest !== plan.planDigest ||
      state.unknownEnvironment !== null ||
      ![test, production].every((row) => row.mutation === "verified")
    )
      throw new Error("completed execution state is inconsistent");
  } else if (state.state === "FailedTest") {
    if (
      state.unknownEnvironment !== null ||
      ![
        "failed",
        "exact_path_absent",
        "unexpected_value",
        "readback_unknown",
      ].includes(test.mutation) ||
      production.mutation !== "pending"
    )
      throw new Error("failed test state is inconsistent");
  } else if (state.state === "Partial") {
    if (
      state.unknownEnvironment !== null ||
      test.mutation !== "verified" ||
      ![
        "failed",
        "exact_path_absent",
        "unexpected_value",
        "readback_unknown",
      ].includes(production.mutation)
    )
      throw new Error("partial execution state is inconsistent");
  }
  return state;
}
