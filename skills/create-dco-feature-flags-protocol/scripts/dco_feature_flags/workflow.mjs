import { randomBytes } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { acquireLock, StateBusyError } from "./lock.mjs";
import { canonicalJson, validatePlan } from "./model.mjs";
import {
  STATE_SCHEMA,
  TERMINAL_STATES,
  validateState,
} from "./state-validation.mjs";
import {
  AmbiguousOutcome,
  ProtocolError,
  effectiveBoolean,
  findToggle,
} from "./protocol.mjs";

export { StateBusyError } from "./lock.mjs";

const targetFor = (plan, environment) =>
  plan.targets.find((target) => target.environment === environment);

async function writeJson(path, value) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${randomBytes(8).toString("hex")}.tmp`;
  try {
    await writeFile(temporary, canonicalJson(value), {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
    await rename(temporary, path);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

function newState(plan) {
  return {
    schema: STATE_SCHEMA,
    planDigest: plan.planDigest,
    state: "New",
    confirmedDigest: null,
    unknownEnvironment: null,
    targets: Object.fromEntries(
      plan.targets.map(({ environment, fullPath }) => [
        environment,
        {
          preflight: "pending",
          mutation: "pending",
          observedValue: null,
          parentPath: null,
          exactPath: fullPath,
          valueTypeId: null,
          toggleId: null,
        },
      ])
    ),
  };
}

async function loadState(path, plan) {
  let state;
  try {
    state = JSON.parse(await readFile(path, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return newState(plan);
    throw new Error("execution state is unreadable", { cause: error });
  }
  return validateState(state, plan);
}

async function readback(transport, target, plan, row) {
  const found = await findToggle(
    transport,
    target,
    target.parentId,
    plan.key,
    target.entityPath
  );
  if (!found) return null;
  if (typeof found.id !== "string" || !found.id)
    throw new ProtocolError("found toggle has no usable id");
  if (row.toggleId !== null && row.toggleId !== found.id)
    throw new ProtocolError("exact-path toggle id differs from create result");
  const detail = await transport.call(target, "getToggle", { id: found.id });
  if (
    !detail ||
    typeof detail !== "object" ||
    detail.id !== found.id ||
    detail.path !== target.entityPath
  )
    throw new ProtocolError("getToggle did not confirm the exact path");
  if (
    detail.type !== "toggle" ||
    detail.parentId !== target.parentId ||
    detail.valueTypeId !== row.valueTypeId ||
    detail.description !== plan.description ||
    detail.onValue !== true ||
    detail.offValue !== false ||
    detail.personalizable !== false ||
    detail.expiredAt !== null ||
    canonicalJson(detail.jsonSchema) !== "{}"
  )
    throw new ProtocolError("getToggle returned unexpected flag configuration");
  const observedValue = effectiveBoolean(detail);
  Object.assign(row, {
    toggleId: found.id,
    observedValue,
    mutation: "verified",
  });
  return observedValue;
}

async function preflight(transport, target, plan, row) {
  const parent = await transport.call(target, "getEntity", {
    id: target.parentId,
  });
  const expectedParentPath = target.createPath.slice(
    0,
    target.createPath.lastIndexOf("/")
  );
  if (
    parent?.id !== target.parentId ||
    parent.type !== "group" ||
    parent.path !== expectedParentPath
  )
    throw new ProtocolError(
      "parent entity does not match the configured group"
    );
  const types = await transport.call(target, "getToggleTypes", {});
  const booleans = Array.isArray(types)
    ? types.filter(
        (item) =>
          item?.name === "boolean" && typeof item.id === "string" && item.id
      )
    : [];
  if (booleans.length !== 1)
    throw new ProtocolError(
      "server did not return exactly one boolean toggle type"
    );
  const existing = await findToggle(
    transport,
    target,
    target.parentId,
    plan.key,
    target.entityPath
  );
  if (existing && (typeof existing.id !== "string" || !existing.id))
    throw new ProtocolError("existing exact-path toggle has no usable id");
  Object.assign(row, {
    preflight: existing ? "duplicate" : "clear",
    parentPath: parent.path,
    valueTypeId: booleans[0].id,
    toggleId: existing?.id ?? null,
  });
}

function createParams(plan, target, row) {
  return {
    name: plan.key,
    path: `${row.parentPath}/${plan.key}`,
    description: plan.description,
    parentId: target.parentId,
    valueTypeId: row.valueTypeId,
    onValue: true,
    offValue: false,
    expiredAt: null,
    isOn: target.expectedValue,
    personalizable: false,
    jsonSchema: {},
  };
}

function receipt(plan, state) {
  return {
    schema: "dco-feature-flag-receipt/1",
    state: state.state,
    planDigest: plan.planDigest,
    application: plan.application,
    key: plan.key,
    protocolBasis: plan.protocolBasis,
    confirmationRequired: [
      "AwaitingConfirmation",
      "AwaitingRenewedConfirmation",
    ].includes(state.state),
    confirmationArgumentRequired: ["ReadyToCreate", "TestVerified"].includes(
      state.state
    ),
    targets: plan.targets.map((target) => {
      const row = state.targets[target.environment];
      return {
        environment: target.environment,
        origin: target.origin,
        fullPath: target.fullPath,
        expectedValue: target.expectedValue,
        preflight: row.preflight,
        mutation: row.mutation,
        observedValue: row.observedValue,
        parentPath: row.parentPath,
        toggleId: row.toggleId,
        createPath: target.createPath,
        protocolEvidence: target.protocolEvidence,
      };
    }),
  };
}

async function preserveUnknownReadback(statePath, state, environment) {
  state.targets[environment].mutation = "started";
  state.state = "UnknownOutcome";
  state.unknownEnvironment = environment;
  await writeJson(statePath, state);
}

async function recoverLegacyReadback(plan, statePath, transport, state) {
  const environment =
    state.targets.test.mutation === "readback_unknown"
      ? "test"
      : state.targets.production.mutation === "readback_unknown"
        ? "production"
        : null;
  if (!environment || !["FailedTest", "Partial"].includes(state.state))
    return false;
  const target = targetFor(plan, environment);
  const row = state.targets[environment];
  let observed;
  try {
    observed = await readback(transport, target, plan, row);
  } catch (error) {
    if (!(error instanceof ProtocolError || error instanceof AmbiguousOutcome))
      throw error;
    return true;
  }
  if (observed === null || observed !== target.expectedValue) {
    row.mutation = observed === null ? "exact_path_absent" : "unexpected_value";
    row.observedValue = observed;
    await writeJson(statePath, state);
    return true;
  }
  state.state = environment === "test" ? "TestVerified" : "Completed";
  state.confirmedDigest = plan.planDigest;
  state.unknownEnvironment = null;
  await writeJson(statePath, state);
  return false;
}

async function executeLocked(plan, statePath, transport, options) {
  const state = await loadState(statePath, plan);
  const legacyReadbackRemainsTerminal = await recoverLegacyReadback(
    plan,
    statePath,
    transport,
    state
  );
  if (legacyReadbackRemainsTerminal) return receipt(plan, state);
  if (TERMINAL_STATES.has(state.state)) {
    if (state.state === "Completed") {
      for (const target of plan.targets) {
        const observed = await readback(
          transport,
          target,
          plan,
          state.targets[target.environment]
        );
        if (observed !== target.expectedValue)
          throw new ProtocolError(
            `${target.environment} readback no longer matches the completed state`
          );
      }
    }
    return receipt(plan, state);
  }

  if (state.state === "UnknownOutcome") {
    const environment = state.unknownEnvironment;
    const target = targetFor(plan, environment);
    const row = state.targets[environment];
    let observed;
    try {
      observed = await readback(transport, target, plan, row);
    } catch (error) {
      if (!(
        error instanceof ProtocolError || error instanceof AmbiguousOutcome
      ))
        throw error;
      await preserveUnknownReadback(statePath, state, environment);
      return receipt(plan, state);
    }
    if (observed === null) {
      if (row.toggleId !== null) {
        state.state = environment === "test" ? "FailedTest" : "Partial";
        state.unknownEnvironment = null;
        row.mutation = "exact_path_absent";
        await writeJson(statePath, state);
        return receipt(plan, state);
      }
      state.state = "AwaitingRenewedConfirmation";
      state.confirmedDigest = null;
      row.mutation = "absent_after_unknown";
      await writeJson(statePath, state);
      return receipt(plan, state);
    }
    if (observed !== target.expectedValue) {
      state.state = environment === "test" ? "FailedTest" : "Partial";
      state.unknownEnvironment = null;
      row.mutation = "unexpected_value";
      await writeJson(statePath, state);
      return receipt(plan, state);
    }
    state.state = environment === "test" ? "TestVerified" : "Completed";
    state.unknownEnvironment = null;
    await writeJson(statePath, state);
  }

  if (state.state === "AwaitingRenewedConfirmation") {
    if (options.renewedConfirmation !== plan.planDigest)
      return receipt(plan, state);
    const environment = state.unknownEnvironment;
    state.targets[environment].mutation = "pending";
    state.state = "ReadyToCreate";
    state.confirmedDigest = plan.planDigest;
    state.unknownEnvironment = null;
    await writeJson(statePath, state);
  }

  if (state.state === "New") {
    for (const target of plan.targets)
      await preflight(
        transport,
        target,
        plan,
        state.targets[target.environment]
      );
    state.state = Object.values(state.targets).some(
      (row) => row.preflight === "duplicate"
    )
      ? "Duplicate"
      : "AwaitingConfirmation";
    await writeJson(statePath, state);
    return receipt(plan, state);
  }

  if (state.state === "AwaitingConfirmation") {
    if (options.confirmation !== plan.planDigest) return receipt(plan, state);
    state.confirmedDigest = plan.planDigest;
    state.state = "ReadyToCreate";
    await writeJson(statePath, state);
  }

  const confirmedNow =
    options.confirmation === plan.planDigest ||
    options.renewedConfirmation === plan.planDigest;
  if (["ReadyToCreate", "TestVerified"].includes(state.state) && !confirmedNow)
    return receipt(plan, state);

  for (const environment of ["test", "production"]) {
    const target = targetFor(plan, environment);
    const row = state.targets[environment];
    if (row.mutation === "verified") continue;
    if (
      environment === "production" &&
      state.targets.test.mutation !== "verified"
    )
      throw new ProtocolError("production is blocked until test is verified");
    state.state = "UnknownOutcome";
    state.unknownEnvironment = environment;
    row.mutation = "started";
    await writeJson(statePath, state);
    let created;
    try {
      created = await transport.call(
        target,
        "createToggle",
        createParams(plan, target, row)
      );
    } catch (error) {
      if (error instanceof AmbiguousOutcome) return receipt(plan, state);
      if (!(error instanceof ProtocolError)) throw error;
      state.state = environment === "test" ? "FailedTest" : "Partial";
      state.unknownEnvironment = null;
      row.mutation = "failed";
      await writeJson(statePath, state);
      return receipt(plan, state);
    }
    if (typeof created?.id !== "string" || !created.id)
      return receipt(plan, state);
    row.toggleId = created.id;
    await writeJson(statePath, state);
    let observed;
    try {
      observed = await readback(transport, target, plan, row);
    } catch (error) {
      if (!(
        error instanceof ProtocolError || error instanceof AmbiguousOutcome
      ))
        throw error;
      await preserveUnknownReadback(statePath, state, environment);
      return receipt(plan, state);
    }
    if (observed === null) {
      state.state = environment === "test" ? "FailedTest" : "Partial";
      state.unknownEnvironment = null;
      row.mutation = "exact_path_absent";
      await writeJson(statePath, state);
      return receipt(plan, state);
    }
    if (observed !== target.expectedValue) {
      state.state = environment === "test" ? "FailedTest" : "Partial";
      state.unknownEnvironment = null;
      row.mutation = "unexpected_value";
      await writeJson(statePath, state);
      return receipt(plan, state);
    }
    state.state = environment === "test" ? "TestVerified" : "Completed";
    state.unknownEnvironment = null;
    await writeJson(statePath, state);
  }
  return receipt(plan, state);
}

export async function execute(planValue, statePath, transport, options = {}) {
  const plan = validatePlan(planValue);
  const release = await acquireLock(statePath);
  try {
    return await executeLocked(plan, statePath, transport, options);
  } finally {
    await release();
  }
}
