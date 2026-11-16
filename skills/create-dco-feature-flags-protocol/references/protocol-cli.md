# Protocol CLI contract

Use this reference when preflighting, creating, or recovering a feature-flag
request.

The CLI supports the exact JSON-RPC route
`/feature-toggle/api/admin/v1/{method}` with Bearer authorization. Its method
set is limited to `getEntity`, `getToggleTypes`, `getEntities`, `createToggle`,
and `getToggle`.

The sanitized L Loans stage exchange observed this complete contract:

1. `createToggle.path` is `smeinterface.l_loans/<key>`;
2. the create response normalizes it to `smeinterface/l_loans/<key>`;
3. exact-path `getEntities` returns the same ID and normalized path;
4. `getToggle` by that ID returns the same path and boolean values.

The plan labels that stage target `observed-create-readback`. Production and
Finance Front are labelled `inferred-from-stage-contract`: they reuse the same
DCO API mapping, while their origins and parent IDs come from the supplied
operational skill. Production traffic was not directly observed.

The first `apply` performs both exact-path preflights and returns before it can
consume confirmation or mutate. A later `apply --confirm <planDigest>` creates
test, verifies its normalized exact path and effective value, then creates and
verifies production. A capture is never a CLI argument and never grants
runtime mutation permission; there are no signing, reviewer, trust-store, or
artificial verification inputs. `plan` accepts `--description-file <path>` as
the file-backed alternative to `--description` for quoted, Unicode, or
multiline text.

The CLI uses the server-returned boolean type UUID and checks duplicates by
the configured slash-form exact path, independent of display name. This lets
legacy state recover readback for toggles whose historical display name differs
from their key. Test remains ordered before production, and production remains
blocked until test readback verifies the expected effective value.

The state file is plain operational history bound to the plan digest. It is
not authorization. A sibling lock serializes applies. Any invocation that
could call `createToggle` must carry `--confirm <planDigest>` in that invocation.
Immediately before a request the CLI writes `UnknownOutcome`. A known create ID
plus unreadable readback stays `UnknownOutcome`; it never authorizes a retry.
Proven absence after an ambiguous create with no returned ID yields
`AwaitingRenewedConfirmation`; retry requires a later
`--renew-confirm <planDigest>`. Reuse the same plan and state paths. A live,
foreign-host, malformed, or otherwise unprovable lock fails closed; never
delete it manually.
