import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { access, mkdtemp, readFile, writeFile } from "node:fs/promises";
import { hostname, tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  buildPlan,
  validatePlan,
} from "../../skills/create-dco-feature-flags-protocol/scripts/dco_feature_flags/model.mjs";
import {
  AmbiguousOutcome,
  HttpRpcTransport,
  ProtocolError,
  findToggle,
} from "../../skills/create-dco-feature-flags-protocol/scripts/dco_feature_flags/protocol.mjs";
import {
  StateBusyError,
  execute,
} from "../../skills/create-dco-feature-flags-protocol/scripts/dco_feature_flags/workflow.mjs";
import { writeLockOwner } from "../../skills/create-dco-feature-flags-protocol/scripts/dco_feature_flags/lock.mjs";

class FakeTransport {
  constructor() {
    this.calls = [];
    this.entities = { test: [], production: [] };
    this.createCount = 0;
    this.readbackFailures = { test: 0, production: 0 };
    this.parentPaths = {
      test: "smeinterface.l_loans",
      production: "smeinterface.l_loans",
    };
  }

  async call(target, method, params) {
    const environment = target.environment;
    this.calls.push([environment, method, params]);
    if (method === "getEntity")
      return {
        id: target.parentId,
        type: "group",
        path: this.parentPaths[environment],
      };
    if (method === "getToggleTypes")
      return [{ id: `boolean-${environment}`, name: "boolean" }];
    if (method === "getEntities") {
      if (
        this.entities[environment].length > 0 &&
        this.readbackFailures[environment] > 0
      ) {
        this.readbackFailures[environment] -= 1;
        throw new AmbiguousOutcome("simulated readback failure");
      }
      return {
        items: this.entities[environment].slice(
          params.offset,
          params.offset + params.limit
        ),
        total: this.entities[environment].length,
      };
    }
    if (method === "createToggle") {
      this.createCount += 1;
      assert.equal(params.path, target.createPath);
      const created = {
        id: `${environment}-created`,
        type: "toggle",
        name: params.name,
        path: target.entityPath,
        parentId: target.parentId,
        isOn: params.isOn,
        onValue: params.onValue,
        offValue: params.offValue,
        valueTypeId: params.valueTypeId,
        description: params.description,
        personalizable: params.personalizable,
        expiredAt: params.expiredAt,
        jsonSchema: params.jsonSchema,
      };
      this.entities[environment].push(created);
      return created;
    }
    if (method === "getToggle")
      return this.entities[environment].find((item) => item.id === params.id);
    throw new Error(`unexpected method ${method}`);
  }
}

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "dco-flags-"));
  return {
    directory,
    state: join(directory, "execution.json"),
    plan: buildPlan("l-loans", "new_flag", "New capability"),
  };
}

test("plan binds the observed create path and labels production inference", async () => {
  const { plan } = await fixture();
  assert.equal(validatePlan(plan), plan);
  assert.equal(plan.projectPath, "/smeinterface/l_loans");
  assert.deepEqual(
    plan.targets.map(
      ({
        fullPath,
        entityPath,
        createPath,
        expectedValue,
        protocolEvidence,
      }) => ({
        fullPath,
        entityPath,
        createPath,
        expectedValue,
        protocolEvidence,
      })
    ),
    [
      {
        fullPath: "/smeinterface/l_loans/new_flag",
        entityPath: "smeinterface/l_loans/new_flag",
        createPath: "smeinterface.l_loans/new_flag",
        expectedValue: true,
        protocolEvidence: "observed-create-readback",
      },
      {
        fullPath: "/smeinterface/l_loans/new_flag",
        entityPath: "smeinterface/l_loans/new_flag",
        createPath: "smeinterface.l_loans/new_flag",
        expectedValue: false,
        protocolEvidence: "inferred-from-stage-contract",
      },
    ]
  );
  assert.equal(
    plan.protocolBasis.stageLLoans.status,
    "observed-create-readback"
  );
  assert.match(plan.protocolBasis.inference, /production and Finance Front/);
});

test("preflight awaits the existing confirmation workflow without mutation", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  const result = await execute(plan, state, transport);
  assert.equal(result.state, "AwaitingConfirmation");
  assert.equal(transport.createCount, 0);
  assert.deepEqual(
    new Set(
      transport.calls
        .filter(([, method]) => method === "getEntities")
        .map(([environment]) => environment)
    ),
    new Set(["test", "production"])
  );
  assert.ok(result.targets.every((row) => row.mutation === "pending"));
  assert.equal(result.targets[0].protocolEvidence, "observed-create-readback");
  assert.equal(
    result.targets[1].protocolEvidence,
    "inferred-from-stage-contract"
  );
  assert.doesNotMatch(
    JSON.stringify(result),
    /"(?:authorization|accessToken|cookie)"\s*:/i
  );
});

test("exact-path duplicate is detected even when its display name differs", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  transport.entities.production.push({
    id: "existing",
    type: "toggle",
    name: "legacy display name",
    path: plan.targets[1].entityPath,
  });
  const result = await execute(plan, state, transport);
  assert.equal(result.state, "Duplicate");
  assert.equal(transport.createCount, 0);
});

test("preflight refuses an unexpected parent path before mutation", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  transport.parentPaths.production = "unexpected.parent";
  await assert.rejects(
    execute(plan, state, transport),
    /parent entity does not match/
  );
  assert.equal(transport.createCount, 0);
});

test("confirmed execution creates stage, verifies it, then creates production", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  assert.equal(
    (await execute(plan, state, transport)).state,
    "AwaitingConfirmation"
  );
  const result = await execute(plan, state, transport, {
    confirmation: plan.planDigest,
  });
  assert.equal(result.state, "Completed");
  assert.equal(transport.createCount, 2);
  const creates = transport.calls.filter(
    ([, method]) => method === "createToggle"
  );
  assert.deepEqual(
    creates.map(([environment, , params]) => [
      environment,
      params.path,
      params.isOn,
    ]),
    [
      ["test", "smeinterface.l_loans/new_flag", true],
      ["production", "smeinterface.l_loans/new_flag", false],
    ]
  );
  const productionCreateIndex = transport.calls.findIndex(
    ([environment, method]) =>
      environment === "production" && method === "createToggle"
  );
  const testReadbackIndex = transport.calls.findIndex(
    ([environment, method]) => environment === "test" && method === "getToggle"
  );
  assert.ok(
    testReadbackIndex >= 0 && testReadbackIndex < productionCreateIndex
  );
});

test("readback rejects incorrect flag configuration before reporting success", async () => {
  const invalid = {
    onValue: false,
    offValue: true,
    valueTypeId: "wrong",
    description: "wrong",
    personalizable: true,
    expiredAt: "tomorrow",
    jsonSchema: { unexpected: true },
    parentId: "wrong",
    type: "group",
  };
  for (const [field, value] of Object.entries(invalid)) {
    const { plan, state } = await fixture();
    const transport = new FakeTransport();
    const originalCall = transport.call.bind(transport);
    transport.call = async (target, method, params) => {
      const result = await originalCall(target, method, params);
      return method === "getToggle" && target.environment === "production"
        ? { ...result, [field]: value }
        : result;
    };
    await execute(plan, state, transport);
    const result = await execute(plan, state, transport, {
      confirmation: plan.planDigest,
    });
    assert.notEqual(result.state, "Completed", field);
  }
});

test("legacy readback_unknown state recovers by exact path despite old display name", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  await execute(plan, state, transport);
  transport.entities.test.push({
    id: "legacy-id",
    type: "toggle",
    name: "old display name",
    path: plan.targets[0].entityPath,
    parentId: plan.targets[0].parentId,
    isOn: true,
    onValue: true,
    offValue: false,
    valueTypeId: "boolean-test",
    description: plan.description,
    personalizable: false,
    expiredAt: null,
    jsonSchema: {},
  });
  const stored = JSON.parse(await readFile(state, "utf8"));
  stored.state = "FailedTest";
  stored.confirmedDigest = plan.planDigest;
  stored.targets.test.mutation = "readback_unknown";
  stored.targets.test.toggleId = "legacy-id";
  await writeFile(state, JSON.stringify(stored), "utf8");
  const result = await execute(plan, state, transport);
  assert.equal(result.state, "TestVerified");
  assert.equal(result.targets[0].mutation, "verified");
  assert.equal(transport.createCount, 0);
});

test("ambiguous post-create readback stays recoverable and never retries create", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  await execute(plan, state, transport);
  transport.readbackFailures.test = 1;
  const unknown = await execute(plan, state, transport, {
    confirmation: plan.planDigest,
  });
  assert.equal(unknown.state, "UnknownOutcome");
  assert.equal(transport.createCount, 1);
  const recovered = await execute(plan, state, transport);
  assert.equal(recovered.state, "TestVerified");
  assert.equal(recovered.confirmationRequired, false);
  assert.equal(recovered.confirmationArgumentRequired, true);
  assert.equal(transport.createCount, 1);
});

test("CLI reports resumable states with the confirmation exit code", async () => {
  const { plan, state, directory } = await fixture();
  const transport = new FakeTransport();
  await execute(plan, state, transport);
  const stored = JSON.parse(await readFile(state, "utf8"));
  stored.state = "ReadyToCreate";
  stored.confirmedDigest = plan.planDigest;
  await writeFile(state, JSON.stringify(stored));
  const planFile = join(directory, "plan.json");
  await writeFile(planFile, JSON.stringify(plan));
  const cli = join(process.cwd(), "scripts", "dco-feature-flags.mjs");
  const check = (expected) => {
    const result = spawnSync(
      process.execPath,
      [cli, "apply", "--plan", planFile, "--state", state],
      { encoding: "utf8" }
    );
    assert.equal(result.status, 3, result.stdout);
    assert.equal(JSON.parse(result.stdout).state, expected);
  };
  check("ReadyToCreate");
  transport.readbackFailures.test = 1;
  await execute(plan, state, transport, { confirmation: plan.planDigest });
  assert.equal((await execute(plan, state, transport)).state, "TestVerified");
  check("TestVerified");
});

test("public CLI has no artifact, review, or trust-store inputs", () => {
  const cli = join(process.cwd(), "scripts", "dco-feature-flags.mjs");
  for (const option of [
    "--evidence",
    "--evidence-artifact",
    "--evidence-review",
  ]) {
    const result = spawnSync(
      process.execPath,
      [cli, "plan", "--key", "x", "--description", "x", option, "x"],
      { encoding: "utf8" }
    );
    assert.equal(result.status, 2);
    assert.match(result.stdout, new RegExp(`unknown option ${option}`));
  }
});

test("plain state is history only and no sibling secret is created", async () => {
  const { plan, state } = await fixture();
  await execute(plan, state, new FakeTransport());
  const stored = JSON.parse(await readFile(state, "utf8"));
  assert.equal(stored.schema, "dco-feature-flag-execution/2");
  assert.equal(stored.state, "AwaitingConfirmation");
  assert.equal(Object.hasOwn(stored, "stateMac"), false);
  await assert.rejects(access(`${state}.integrity-key`), /ENOENT/);
});

test("unsupported or forged persisted states fail structural validation", async () => {
  const unsupported = await fixture();
  await execute(unsupported.plan, unsupported.state, new FakeTransport());
  const state = JSON.parse(await readFile(unsupported.state, "utf8"));
  state.state = "Invented";
  await writeFile(unsupported.state, JSON.stringify(state), "utf8");
  await assert.rejects(
    execute(unsupported.plan, unsupported.state, new FakeTransport()),
    /execution state is unknown/
  );

  const forged = await fixture();
  await execute(forged.plan, forged.state, new FakeTransport());
  const forgedState = JSON.parse(await readFile(forged.state, "utf8"));
  forgedState.state = "Completed";
  await writeFile(forged.state, JSON.stringify(forgedState), "utf8");
  await assert.rejects(
    execute(forged.plan, forged.state, new FakeTransport()),
    /[Cc]ompleted execution state is inconsistent/
  );
});

test("a lock owned by a live process fails closed", async () => {
  const { plan, state } = await fixture();
  const transport = new FakeTransport();
  await writeLockOwner(`${state}.lock`, {
    pid: process.pid,
    hostname: hostname(),
    createdAt: new Date().toISOString(),
    nonce: "0".repeat(32),
  });
  await assert.rejects(execute(plan, state, transport), StateBusyError);
  assert.equal(transport.calls.length, 0);
});

test("CLI rejects unknown, duplicate, missing, and conflicting options", () => {
  const cli = join(process.cwd(), "scripts", "dco-feature-flags.mjs");
  const cases = [
    [
      [
        "plan",
        "--aplication",
        "finance-front",
        "--key",
        "x",
        "--description",
        "x",
      ],
      /unknown option --aplication/,
    ],
    [
      ["plan", "--key", "x", "--key", "y", "--description", "x"],
      /duplicate option --key/,
    ],
    [[], /usage:/],
    [
      [
        "apply",
        "--plan",
        "x",
        "--state",
        "y",
        "--confirm",
        "a",
        "--renew-confirm",
        "b",
      ],
      /mutually exclusive/,
    ],
  ];
  for (const [args, pattern] of cases) {
    const result = spawnSync(process.execPath, [cli, ...args], {
      encoding: "utf8",
    });
    assert.equal(result.status, 2);
    assert.match(result.stdout, pattern);
  }
});

test("HTTP 5xx is ambiguous while HTTP 4xx is definitive", async (context) => {
  const originalFetch = globalThis.fetch;
  context.after(() => {
    globalThis.fetch = originalFetch;
  });
  const transport = new HttpRpcTransport({ test: "not-logged" });
  const target = { environment: "test", origin: "https://example.invalid" };
  globalThis.fetch = async () => new Response("{}", { status: 503 });
  await assert.rejects(
    transport.call(target, "createToggle", {}),
    AmbiguousOutcome
  );
  globalThis.fetch = async () => new Response("{}", { status: 403 });
  await assert.rejects(
    transport.call(target, "createToggle", {}),
    ProtocolError
  );
});

test("entity search retains observed pagination and exact-path matching", async () => {
  const { plan } = await fixture();
  const transport = new FakeTransport();
  transport.entities.test = Array.from({ length: 13 }, (_, index) => ({
    id: String(index),
    type: "toggle",
    name: `flag_${index}`,
    path: `smeinterface/l_loans/flag_${index}`,
  }));
  const found = await findToggle(
    transport,
    plan.targets[0],
    plan.targets[0].parentId,
    "flag_12",
    "smeinterface/l_loans/flag_12"
  );
  assert.equal(found.id, "12");
  const calls = transport.calls.filter(
    ([, method]) => method === "getEntities"
  );
  assert.deepEqual(
    calls.map(([, , params]) => params.offset),
    [0, 10]
  );
  assert.ok(
    calls.every(
      ([, , params]) =>
        params.limit === 10 &&
        params.filters.withoutArchived === false &&
        params.filters.query === "flag_12"
    )
  );
});
