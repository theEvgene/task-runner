# Submit syntax gate

Opt-in, syntax-only pre-check of one signed LAN command before it is sealed
and published. It is part of `submit` (`docs/lan-bridge.md`), which owns the
client, the observation rule, and the confirmed-state classification; this
document owns only the gate.

With `--dialect powershell|cmd`, `submit` checks the command syntax
before seal/publish through the small seam
`scripts/lan_bridge/syntax.py::validate(command, dialect)`. The check
point sits in `submit_job` ahead of JOBID generation: a refusal prints
the reason to stderr, exits `2`, and leaves no JOBID and no file under
`jobs/`. A pass continues down the unchanged
seal/publish/MAC/immutability path.

- Explicit dialect, no guessing. The dialect must be `powershell` or
  `cmd`; anything else refuses naming the explicit choice. Without
  `--dialect` the gate is off and the submit path is unchanged.
- Worker shell compatibility. The worker executes through the platform
  shell (`shell=True`; `COMSPEC`/`cmd.exe` on Windows), so a bare
  PowerShell line is not portable. `--dialect powershell` requires an
  explicit launcher: `powershell -Command <script>` (or
  `powershell.exe`/`pwsh`, with optional launcher flags). A bare line
  such as `& '...' ...` refuses as ambiguous even when its PowerShell
  syntax is valid; wrap it as
  `powershell -NoProfile -Command "& '...' ..."`. Only one `-Command`
  script argument is accepted: no unquoted cmd operator
  (`| & < > ^ ( )`) may sit outside a double-quoted script, no cmd
  token may follow its closing quote, and `-File`/other launcher shapes
  refuse. This is a launcher-shape check, not a full cmd parse of the
  launcher.
- `powershell` runs `Parser.ParseInput` (AST + `Token[]` +
  `ParseError[]`) on the inner `-Command` script; a syntax error
  refuses with the `powershell` dialect, a secret-free parser reason,
  and the source offset within the submitted command file. `cmd` is a
  deliberately narrow subset: one command line, balanced double quotes,
  no NUL, and no unquoted `| & < > ^ ( )` operator. That refuses
  pipelines, redirection, compounds, and subshells, so malformed forms
  such as `echo hi |` refuse; it is not a full cmd grammar check.
- Syntax only. A pass says nothing about the runtime: cmdlet/path
  existence, execution policy, providers, rights, and the network are
  not checked. A valid line naming a nonexistent cmdlet or path passes,
  and a pass must never be read as "exists" or "safe". The gate is not
  a security sanitizer or an allow-list.
- Fail-closed when active. If the validator cannot run (no
  `pwsh`/`powershell` runtime, nonzero parser-probe exit even with
  output, or unreadable probe output), submit refuses before any write
  instead of skipping the check. The operator may pin or blank
  discovery with `TASK_RUNNER_POWERSHELL_BIN` (a path, or empty to
  force unavailable). Without `--dialect` the gate stays off and the
  submit path keeps its legacy behavior.
- Diagnostics never carry secrets: only the dialect, a secret-free
  reason (parser free text and caught-exception text pass through a
  redactor that removes command-derived tokens of any length, including
  short ones, and truncates), and the offset, plus the usual call
  parameters. Command text, key material and queue contents are never
  printed.

The gate has two suites: `tests/lan_bridge/test_submit_syntax_gate.py`
(seam via stub adapters, ambiguous dialect, fail-closed validator,
secret-free diagnostics, legacy pass-through) and
`tests/lan_bridge/test_powershell_syntax.py` (`Parser.ParseInput` refusals
with offset, the wrapped `& '...'` shape, launcher-boundary refusals
including an external cmd operator, the nonexistent-cmdlet negative case,
nonzero-probe fail-closed, and a missing PowerShell backend). No second
host is ever reached.

## Writer rendering

`scripts/dell_lan/dell_request.py` renders the one command a Dell request
defines, and the request records the declared dialect so the managed submit
re-checks it instead of trusting the command file. The writer never infers the
dialect.

- `powershell` builds a literal program beginning with the call operator
  (`&`); the writer then applies the one supported explicit
  `powershell.exe -Command` launcher for signed submit and worker execution.
- `cmd` uses the quoted Windows command form. Cmd arguments containing `%` or
  `!` expansion markers, control characters, or an embedded double quote are
  unrepresentable and refuse before either file is written.
- PowerShell single-quoted arguments preserve `%` and `!`; the worker replaces
  the validated launcher with an encoded wrapper before crossing `cmd.exe`. An
  embedded double quote in a PowerShell argument still refuses at transport
  adaptation because it would close the supported launcher's script quote.
  Diagnostics name only the dialect and fixed reason.
