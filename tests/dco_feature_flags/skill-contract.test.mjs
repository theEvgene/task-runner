import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL(
  "../../skills/create-dco-feature-flags-protocol/",
  import.meta.url
);

test("skill routes execution and recovery through the protocol CLI", async () => {
  const text = await readFile(new URL("SKILL.md", root), "utf8");
  assert.match(text, /scripts\/dco-feature-flags\.mjs/);
  assert.match(text, /references\/protocol-cli\.md/);
  assert.match(text, /UnknownOutcome/);
  assert.match(text, /AwaitingRenewedConfirmation/);
  assert.match(text, /DCO_TEST_ACCESS_TOKEN/);
  assert.match(text, /relative to this/);
  assert.match(text, /protocolBasis/);
  assert.match(text, /observed/);
  assert.match(text, /inference/);
  assert.match(text, /runtime does not ingest a capture/);
});

test("skill remains model-invoked and has an explicit default prompt", async () => {
  const skill = await readFile(new URL("SKILL.md", root), "utf8");
  assert.match(
    skill,
    /^---\r?\nname: create-dco-feature-flags-protocol\r?\ndescription:/
  );
  const metadata = await readFile(new URL("agents/openai.yaml", root), "utf8");
  assert.match(metadata, /\$create-dco-feature-flags-protocol/);
});

test("protocol skill carries a CLI with no trust or signing mechanism", async () => {
  const cli = await readFile(
    new URL("scripts/dco-feature-flags.mjs", root),
    "utf8"
  );
  const model = await readFile(
    new URL("scripts/dco_feature_flags/model.mjs", root),
    "utf8"
  );
  const workflow = await readFile(
    new URL("scripts/dco_feature_flags/workflow.mjs", root),
    "utf8"
  );
  for (const source of [cli, model, workflow]) {
    assert.doesNotMatch(
      source,
      /trusted.?review|ed25519|signature|createHmac|integrity-key/i
    );
  }
});
