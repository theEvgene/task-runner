#!/usr/bin/env node
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { buildPlan, canonicalJson } from "./dco_feature_flags/model.mjs";
import {
  AmbiguousOutcome,
  HttpRpcTransport,
  ProtocolError,
} from "./dco_feature_flags/protocol.mjs";
import { execute } from "./dco_feature_flags/workflow.mjs";

const EXIT_BY_STATE = {
  Completed: 0,
  AwaitingConfirmation: 3,
  ReadyToCreate: 3,
  TestVerified: 3,
  Duplicate: 4,
  UnknownOutcome: 5,
  AwaitingRenewedConfirmation: 5,
  FailedTest: 6,
  Partial: 7,
};

const OPTIONS_BY_COMMAND = {
  plan: new Set([
    "application",
    "key",
    "description",
    "description-file",
    "out",
  ]),
  apply: new Set(["plan", "state", "timeout-ms", "confirm", "renew-confirm"]),
};

function parse(argv) {
  const command = argv.shift();
  if (!["plan", "apply"].includes(command))
    throw new Error("usage: dco-feature-flags.mjs <plan|apply> [options]");
  const options = { command };
  const allowed = OPTIONS_BY_COMMAND[command];
  while (argv.length) {
    const name = argv.shift();
    if (!name?.startsWith("--") || name === "--" || argv.length === 0)
      throw new Error(`missing value for ${name}`);
    const key = name.slice(2);
    if (!allowed.has(key)) throw new Error(`unknown option --${key}`);
    if (Object.hasOwn(options, key))
      throw new Error(`duplicate option --${key}`);
    if (argv[0].startsWith("--")) throw new Error(`missing value for ${name}`);
    options[key] = argv.shift();
  }
  return options;
}

function required(options, name) {
  if (!options[name]) throw new Error(`--${name} is required`);
  return options[name];
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

async function main(argv) {
  const options = parse([...argv]);
  if (options.command === "plan") {
    const application = options.application ?? "l-loans";
    if (options.description !== undefined && options["description-file"] !== undefined)
      throw new Error("--description and --description-file are mutually exclusive");
    let description = options.description;
    if (options["description-file"] !== undefined)
      description = await readFile(options["description-file"], "utf8");
    const plan = buildPlan(
      application,
      required(options, "key"),
      required({ description }, "description")
    );
    if (options.out) {
      await mkdir(dirname(options.out), { recursive: true });
      await writeFile(options.out, canonicalJson(plan), {
        encoding: "utf8",
        flag: "wx",
      });
    }
    emit(plan);
    return 0;
  }
  if (options.confirm && options["renew-confirm"])
    throw new Error("--confirm and --renew-confirm are mutually exclusive");
  const plan = JSON.parse(await readFile(required(options, "plan"), "utf8"));
  const timeoutMs = Number(options["timeout-ms"] ?? 20_000);
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120_000)
    throw new Error("--timeout-ms must be an integer from 1 to 120000");
  const shared = process.env.DCO_ACCESS_TOKEN ?? "";
  const transport = new HttpRpcTransport(
    {
      test: process.env.DCO_TEST_ACCESS_TOKEN ?? shared,
      production: process.env.DCO_PRODUCTION_ACCESS_TOKEN ?? shared,
    },
    timeoutMs
  );
  const result = await execute(plan, required(options, "state"), transport, {
    confirmation: options.confirm,
    renewedConfirmation: options["renew-confirm"],
  });
  emit(result);
  return EXIT_BY_STATE[result.state] ?? 2;
}

try {
  process.exitCode = await main(process.argv.slice(2));
} catch (error) {
  if (!(error instanceof Error)) throw error;
  emit({
    schema: "dco-feature-flag-error/1",
    state: "Error",
    message: error.message,
  });
  process.exitCode =
    error instanceof ProtocolError || error instanceof AmbiguousOutcome ? 2 : 2;
}
