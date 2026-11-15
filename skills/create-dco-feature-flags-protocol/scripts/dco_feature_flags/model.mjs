import { createHash } from "node:crypto";

export const PLAN_SCHEMA = "dco-feature-flag-plan/3";
const KEY_PATTERN = /^[a-z][a-z0-9_]*$/;

// Target bindings come from the supplied operational skill. The request and
// normalized response path forms were directly observed only for L Loans on
// stage; the other targets use that service contract as an explicit inference.
const APPLICATIONS = {
  "l-loans": {
    displayName: "L Loans UI",
    projectPath: "/smeinterface/l_loans",
    targets: [
      [
        "test",
        "https://adm-stage.dev-tcsgroup.io",
        "2e4d0a84-461a-4f37-b6c8-1d44415c32c8",
        true,
        "observed-stage-protocol",
        "observed-create-readback",
      ],
      [
        "production",
        "https://adm.tinkoff.ru",
        "130d6864-7a20-4259-8279-4f525699906c",
        false,
        "supplied-operational-skill",
        "inferred-from-stage-contract",
      ],
    ],
  },
  "finance-front": {
    projectPath: "/smeinterface/rko_accounts",
    displayName: "Finance Front (Счета и платежи)",
    targets: [
      [
        "test",
        "https://adm-stage.dev-tcsgroup.io",
        "db720c6d-8d4d-4f6c-b452-e47f3b0bc647",
        true,
        "supplied-operational-skill",
        "inferred-from-stage-contract",
      ],
      [
        "production",
        "https://adm.tinkoff.ru",
        "770b1be9-ee66-4919-979a-f57037589452",
        false,
        "supplied-operational-skill",
        "inferred-from-stage-contract",
      ],
    ],
  },
};

function sorted(value) {
  if (Array.isArray(value)) return value.map(sorted);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sorted(value[key])])
    );
  }
  return value;
}

export function canonicalJson(value) {
  return JSON.stringify(sorted(value));
}

export function digest(value) {
  return createHash("sha256")
    .update(canonicalJson(value), "utf8")
    .digest("hex");
}

export function buildPlan(application, rawKey, rawDescription) {
  const key = rawKey.trim();
  const description = rawDescription.trim();
  const configured = APPLICATIONS[application];
  if (!configured)
    throw new Error("application must be l-loans or finance-front");
  if (!KEY_PATTERN.test(key)) {
    throw new Error(
      "key must start with a lowercase letter and contain only lowercase letters, digits, and underscores"
    );
  }
  if (!description) throw new Error("description must not be empty");
  const plan = {
    schema: PLAN_SCHEMA,
    application: { id: application, displayName: configured.displayName },
    key,
    description,
    valueType: "boolean",
    personalizable: false,
    expiresAt: null,
    projectPath: configured.projectPath,
    targets: configured.targets.map(
      ([
        environment,
        origin,
        parentId,
        expectedValue,
        bindingSource,
        protocolEvidence,
      ]) => ({
        environment,
        origin,
        parentId,
        projectPath: configured.projectPath,
        fullPath: `${configured.projectPath}/${key}`,
        entityPath: `${configured.projectPath.slice(1)}/${key}`,
        createPath: `${configured.projectPath.slice(1).replace("/", ".")}/${key}`,
        expectedValue,
        bindingSource,
        protocolEvidence,
      })
    ),
    protocolBasis: {
      stageLLoans: {
        status: "observed-create-readback",
        capturedAt: "2026-11-15T10:12:06.923Z",
        createPathForm: "dotted-parent/slash-key",
        responsePathForm: "slash-separated",
      },
      inference:
        "production and Finance Front use the observed DCO API path contract with bindings supplied by the existing operational skill",
    },
  };
  return { ...plan, planDigest: digest(plan) };
}

export function validatePlan(plan) {
  if (!plan || typeof plan !== "object" || plan.schema !== PLAN_SCHEMA)
    throw new Error("unsupported plan schema");
  const { planDigest, ...content } = plan;
  if (typeof planDigest !== "string" || digest(content) !== planDigest)
    throw new Error("plan digest does not match its contents");
  const rebuilt = buildPlan(
    plan.application?.id ?? "",
    String(plan.key ?? ""),
    String(plan.description ?? "")
  );
  if (canonicalJson(plan) !== canonicalJson(rebuilt))
    throw new Error("plan differs from supported application configuration");
  return plan;
}
