# Lenovo default-playback mute/read over the signed LAN bridge

Agent entry. Use this route when the task needs the mute state of the
default Windows playback endpoint on Lenovo: mute it, or read whether it is
muted. The agent never hand-assembles the PowerShell/COM command:
`scripts/lenovo_audio/audio.py` renders the exact signed command, and its
receipt parser decides success only after the target state was read back.

Do NOT use it for: volume levels, other devices or roles, any second
bridge/worker/service, a universal command platform, or Jira/DCO work.

## What it does

- `mute` sets mute to true on the default render endpoint
  (eRender + eMultimedia) **unconditionally**, then reads the state back in
  the same run and reports it. It never toggles, so a repeated `mute`
  cannot unmute.
- `read` only reads the current mute state and reports it. No state change.

Both run as one signed job through the existing transport
(`docs/lan-bridge.md`): `lan_bridge.py submit` with `--dialect powershell`,
then `fetch` or `get.py` on the SAME id. No new client, no new queue.

Lenovo requirements: PowerShell with FullLanguage and `Add-Type` permitted
(the operation compiles a small MMDevice/CoreAudio interop assembly in
memory; nothing is installed). A host that blocks the spawn seals a terminal
`error` envelope (see below) instead of running anything.

## Files

- `scripts/lenovo_audio/audio.py` — builder, receipt parser, verdict, and
  the `build` subcommand. Writes the command file only; never submits,
  never touches the queue, key, or Lenovo.
- `tests/lenovo_audio/test_audio.py` — fast hermetic contract tests.

## Inputs (owner-provided, never invented)

- `--operation mute|read`.
- The signed queue dir and 32-byte key file (outside every checkout and
  outside the queue), exactly as `docs/lan-bridge.md` requires.
- The Lenovo working directory for `submit --cwd` (any existing directory;
  the command touches no files, so the checkout is the usual choice).
- Timeouts: remote execution limit `--timeout 60` is ample (the run is
  instant); result wait uses the live settings (`fetch --timeout 600
  --poll 30`).

No secrets, tokens, or user absolute paths travel in the job: the command
carries only the interop program and the operation name.

## Exact commands

Placeholders only; never commit real queue, key, or host paths.

1. Render the command file on Dell (writes files only, no submission):

```text
python scripts/lenovo_audio/audio.py build --operation mute --out <dell-temp>\audio-mute.txt
python scripts/lenovo_audio/audio.py build --operation read --out <dell-temp>\audio-read.txt
```

2. Submit exactly one job through the existing signed transport:

```text
python scripts/lan_bridge/lan_bridge.py submit \
  --queue <queue-dir> --key-file <key-path> \
  --command-file <dell-temp>\audio-<mute|read>.txt \
  --cwd <lenovo-checkout> --timeout 60 --max-output 65536 \
  --dialect powershell
```

The submit prints `JOBID=<32hex>`. Keep that id: it is the only
continuation key.

3. Observe the SAME id until a MAC-verified result bound to it arrives:

```text
python scripts/lan_bridge/get.py \
  --queue <queue-dir> --key-file <key-path> \
  --id <JOBID> --window 60 --poll 30
```

(`fetch --id <JOBID>` is the equivalent single wait; both never publish.)
On bounded-wait expiry the outcome is unknown: re-read the SAME id with
`fetch --id` or `get.py --id`. Never submit a second job while the outcome is
unknown; the queue refuses duplicates and a second submit would be a NEW
id, not a retry.

## Receipt and success rule

Stdout on success is exactly one JSON object, schema
`lenovo-audio-state/1`:

```text
{"schema":"lenovo-audio-state/1","operation":"mute","muted":true,"ok":true}
```

On a Lenovo-side failure stdout carries the same schema with `"ok":false`
and a bounded token (`no-default-playback-device`, `audio-operation-failed`):

```text
{"schema":"lenovo-audio-state/1","operation":"read","ok":false,"error":"no-default-playback-device"}
```

Confirmed success needs ALL of: a MAC-verified envelope bound to the id,
no worker timeout, no truncation, a recognized status, an exact zero exit,
a bound receipt with `ok` true — and `muted` true when muting. Anything
else is never success:

| Observed state | Verdict | Next action |
| --- | --- | --- |
| receipt `ok` true, `muted` matches the operation | `success` | consume the verified state |
| `mute` receipt `muted` false; receipt `ok` false; nonzero exit | `worker_failure` | fix the Lenovo endpoint/inputs, then a NEW id |
| verified envelope, unparsable or foreign stdout | `unknown_receipt` | NEW id, never success |
| worker timeout / truncated output / unrecognized status | terminal, per `wait.classify_result` | NEW id (larger limits for truncation) |
| no verified result yet (bounded wait expired) | unknown | SAME id via `fetch`/`get.py`, never a resubmit |
| host blocked the spawn (verified `error`, empty stdout) | `unknown_status` | inspect worker result, fix host policy, then a NEW id |

A repeated `mute` that reports `muted` true is the idempotent steady
state, not a toggle. `decide` in `audio.py` implements exactly this table;
transport wording stays owned by `wait.classify_result`.

## Boundaries

- Default playback endpoint only (eRender + eMultimedia). No device
  selection, no volume level control, no session mute.
- Lenovo execution — including the read-only `read` — runs only with the
  current session's explicit authorization; `mute` additionally never runs
  on assumption. Live sound changes only with permission in that session.
- The command, receipts, and reports carry no key material, tokens, or
  user absolute paths. Never paste worker stderr or queue contents into a
  report; the receipt error token is the whole diagnosis.
- A future transferable executable may replace the inline program, but
  porting the language is out of scope for this route.

## Local verification

```text
<python> -m unittest discover -s tests/lenovo_audio -v
<python> -m unittest discover -s tests/lan_bridge -p "test_lan_bridge_status.py" -v
node scripts/check-file-tokens.mjs
```

The suite seals fixtures with an ephemeral key (MAC binding, tamper
refusal, same-id recovery shape included) and asserts both commands pass
the real PowerShell syntax gate when a runtime exists. No host, share,
queue, key file, or live audio change is touched.
