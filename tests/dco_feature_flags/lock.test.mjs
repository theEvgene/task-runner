import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtemp, readFile } from "node:fs/promises";
import { hostname, tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  acquireLock,
  writeLockOwner,
} from "../../skills/create-dco-feature-flags-protocol/scripts/dco_feature_flags/lock.mjs";

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "dco-lock-"));
  return { directory, state: join(directory, "execution.json") };
}

function staleOwner() {
  return {
    pid: 999999,
    hostname: hostname(),
    createdAt: new Date(Date.now() - 60_000).toISOString(),
    nonce: randomBytes(16).toString("hex"),
  };
}

const lockModule = new URL(
  "../../skills/create-dco-feature-flags-protocol/scripts/dco_feature_flags/lock.mjs",
  import.meta.url
).href;

test("competing stale recoverers never overlap critical sections", async () => {
  const { directory, state } = await fixture();
  await writeLockOwner(`${state}.lock`, staleOwner());
  const script = `
    import { appendFile } from "node:fs/promises";
    import { acquireLock, StateBusyError } from ${JSON.stringify(lockModule)};
    try {
      const release = await acquireLock(process.argv[1]);
      await appendFile(process.argv[2], JSON.stringify({kind:"acquire",pid:process.pid,at:Date.now()})+"\\n");
      await new Promise(resolve => setTimeout(resolve, 250));
      await appendFile(process.argv[2], JSON.stringify({kind:"release",pid:process.pid,at:Date.now()})+"\\n");
      await release();
    } catch (error) {
      if (!(error instanceof StateBusyError)) throw error;
    }
  `;
  const eventsPath = join(directory, "events.jsonl");
  const children = Array.from({ length: 16 }, () =>
    spawn(process.execPath, [
      "--input-type=module",
      "--eval",
      script,
      state,
      eventsPath,
    ])
  );
  await Promise.all(
    children.map(
      (child) =>
        new Promise((resolve, reject) => {
          child.on("error", reject);
          child.on("exit", (code) =>
            code === 0 ? resolve() : reject(new Error(`child exited ${code}`))
          );
        })
    )
  );
  const events = (await readFile(eventsPath, "utf8"))
    .trim()
    .split("\n")
    .filter(Boolean)
    .map(JSON.parse);
  let active = 0;
  let maximum = 0;
  for (const event of events.sort((a, b) => a.at - b.at)) {
    active += event.kind === "acquire" ? 1 : -1;
    maximum = Math.max(maximum, active);
  }
  assert.ok(events.some((event) => event.kind === "acquire"));
  assert.equal(maximum, 1);
  assert.equal(active, 0);
});

test("a process killed after stale quarantine cannot strand recovery", async () => {
  const { state } = await fixture();
  await writeLockOwner(`${state}.lock`, staleOwner());
  const child = spawn(
    process.execPath,
    [
      "--input-type=module",
      "--eval",
      `import { acquireLock } from ${JSON.stringify(lockModule)};
       await acquireLock(process.argv[1], {afterQuarantine: async () => {
         process.stdout.write("quarantined\\n");
         await new Promise(() => {});
       }});`,
      state,
    ],
    { stdio: ["ignore", "pipe", "pipe"] }
  );
  await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.stdout.once("data", (chunk) =>
      String(chunk).includes("quarantined")
        ? resolve()
        : reject(new Error(`unexpected child output: ${chunk}`))
    );
  });
  child.kill();
  await new Promise((resolve) => child.once("exit", resolve));
  const release = await acquireLock(state);
  await release();
});
