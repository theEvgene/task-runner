import { randomBytes } from "node:crypto";
import {
  mkdir,
  readFile,
  rename,
  rm,
  stat,
  unlink,
  writeFile,
} from "node:fs/promises";
import { hostname } from "node:os";
import { dirname, join } from "node:path";

export class StateBusyError extends Error {}

const ownerName = "owner.json";
const maxRaceRetries = 64;

function newMetadata() {
  return {
    pid: process.pid,
    hostname: hostname(),
    createdAt: new Date().toISOString(),
    nonce: randomBytes(16).toString("hex"),
  };
}

function validMetadata(value) {
  return (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).sort().join(",") ===
      ["createdAt", "hostname", "nonce", "pid"].sort().join(",") &&
    Number.isInteger(value.pid) &&
    value.pid > 0 &&
    typeof value.hostname === "string" &&
    value.hostname.length > 0 &&
    typeof value.createdAt === "string" &&
    Number.isFinite(Date.parse(value.createdAt)) &&
    Date.parse(value.createdAt) <= Date.now() &&
    typeof value.nonce === "string" &&
    /^[a-f0-9]{32}$/.test(value.nonce)
  );
}

function processIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") return false;
    return true;
  }
}

function busyFor(metadata) {
  if (!validMetadata(metadata))
    return "execution lock metadata is invalid; no action was taken";
  if (metadata.hostname !== hostname())
    return "execution lock belongs to another host; no action was taken";
  if (processIsAlive(metadata.pid))
    return "execution state is locked by a live apply; no action was taken";
  return null;
}

async function readOwner(lockPath, kind) {
  try {
    const raw =
      kind === "directory"
        ? await readFile(join(lockPath, ownerName), "utf8")
        : await readFile(lockPath, "utf8");
    return JSON.parse(raw);
  } catch (error) {
    throw new StateBusyError(
      `execution lock cannot be proven stale: ${error.message}`
    );
  }
}

async function quarantineStaleLock(lockPath, kind, hooks) {
  const metadata = await readOwner(lockPath, kind);
  const blocked = busyFor(metadata);
  if (blocked) throw new StateBusyError(blocked);

  // Renaming the whole lock is the stale-owner election. Exactly one process
  // can detach the stable name. A crash after this point leaves only a uniquely
  // named quarantine, so it can never strand future acquisition.
  const quarantinePath = `${lockPath}.stale-${randomBytes(16).toString("hex")}`;
  try {
    await rename(lockPath, quarantinePath);
  } catch (error) {
    if (["ENOENT", "EEXIST", "EPERM"].includes(error.code)) return false;
    throw error;
  }
  await hooks.afterQuarantine?.(quarantinePath);
  await rm(quarantinePath, { recursive: true, force: true });
  return true;
}

async function lockKind(lockPath) {
  try {
    return (await stat(lockPath)).isDirectory() ? "directory" : "legacy-file";
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

export async function acquireLock(statePath, hooks = {}) {
  const lockPath = `${statePath}.lock`;
  await mkdir(dirname(lockPath), { recursive: true });
  let raceRetries = 0;

  for (;;) {
    const metadata = newMetadata();
    const candidatePath = `${lockPath}.candidate-${metadata.nonce}`;
    try {
      await mkdir(candidatePath);
      await writeFile(
        join(candidatePath, ownerName),
        JSON.stringify(metadata),
        { encoding: "utf8", flag: "wx", mode: 0o600 }
      );
      await rename(candidatePath, lockPath);
    } catch (error) {
      await rm(candidatePath, { recursive: true, force: true });
      if (!["EEXIST", "ENOTEMPTY", "EPERM"].includes(error.code)) throw error;
      const kind = await lockKind(lockPath);
      if (kind === null) {
        raceRetries += 1;
        if (raceRetries > maxRaceRetries)
          throw new StateBusyError(
            "execution lock changed repeatedly during acquisition"
          );
        continue;
      }
      const recovered = await quarantineStaleLock(lockPath, kind, hooks);
      raceRetries = recovered ? 0 : raceRetries + 1;
      if (raceRetries > maxRaceRetries)
        throw new StateBusyError(
          "execution lock changed repeatedly during stale recovery"
        );
      continue;
    }

    return async () => {
      const current = await readOwner(lockPath, "directory");
      if (current.nonce !== metadata.nonce)
        throw new StateBusyError(
          "execution lock ownership changed; lock retained"
        );
      await unlink(join(lockPath, ownerName));
      await rm(lockPath, { recursive: true });
    };
  }
}

export async function writeLockOwner(lockPath, metadata) {
  await mkdir(lockPath, { recursive: false });
  await writeFile(join(lockPath, ownerName), JSON.stringify(metadata), {
    encoding: "utf8",
    flag: "wx",
    mode: 0o600,
  });
}
