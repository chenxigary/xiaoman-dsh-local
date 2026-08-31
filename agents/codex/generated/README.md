# Pinned stable Codex protocol artifacts

These files are generated from the exact audited Codex CLI build
`0.149.0-alpha.4.1` with the stable generator surface only. They are checked in
so a clean checkout can verify the protocol contract without relying on a
developer's current Codex installation.

The source commands executed by the gate are, in this exact order:

```bash
CODEX_BIN=/absolute/path/to/discovered/codex
schema_output=/absolute/tool-owned-output
"$CODEX_BIN" --version
"$CODEX_BIN" app-server generate-json-schema --out "$schema_output/stable-json"
"$CODEX_BIN" app-server generate-ts --out "$schema_output/stable-ts"
```

The version command must print exactly `codex-cli 0.149.0-alpha.4.1` followed by
one newline, with no stderr. A mismatched or malformed banner is rejected
before the gate creates output or invokes either generator. Do not add
`--experimental`; experimental output is intentionally not checked in and is
not part of the runtime surface. Here `schema_output` denotes the fresh child
created by the gate. The stable generator may still emit a small
number of definitions whose upstream names/descriptions contain
`ExperimentalFeature`; those are stable artifact vocabulary, not evidence
that the experimental generator flag was enabled. Runtime code must keep an
explicit method allowlist. The generated JSON tree contains 291 files;
the generated TypeScript tree contains 663 files and is compiled through its
generated `index.ts` entrypoint.

Run the reproducible gate from the repository root:

```bash
python3 scripts/codex-schema-gate.py check
python3 scripts/codex-schema-gate.py compile \
  --tsc .runtime/deepseek-harness/node_modules/.bin/tsc
```

To regenerate into a fresh directory and compare it with this checkout:

```bash
repo_root="$(pwd -P)"
schema_parent="$repo_root/.run"
if [ -L "$schema_parent" ]; then
  echo "refusing symlinked schema parent" >&2
  exit 1
fi
mkdir -p "$schema_parent"
schema_output="$schema_parent/codex-schema-stable-generated"
python3 scripts/codex-schema-gate.py generate \
  --codex "$CODEX_BIN" \
  --out "$schema_output"
python3 scripts/codex-schema-gate.py check \
  --generated "$schema_output"
```

`--out` names a child that must not exist. The gate checks that child and all
existing ancestors for symlinks, creates the child itself, and then creates
its empty `stable-json` and `stable-ts` directories. This intentionally
rejects macOS aliases such as a lexical `/tmp` path; use the physical
repository path shown above.

The exact bundle, JSON tree, and TypeScript tree digests are recorded in
`../protocol-manifest.json`. No credentials, account data, or app-server
frames are part of these artifacts.

At runtime, `schema_validator.py` re-verifies the v2 bundle SHA-256 and the
complete generated JSON-tree digest before constructing Draft 7 validators.
Outbound capability is intentionally narrower than the generated
`ClientRequest` union: only the nine methods in `requiredWire.clientRequests`
and the `initialized` notification are accepted, with additional DSH policy
checks that keep experimental API, attestation, token refresh, API-key login,
write sandbox, and approvals disabled. A pending response is validated with
the result schema for its original request method. Server requests and
notifications first pass their generated envelope; known and future
well-typed requests receive only a fixed denial that retains at most bounded
routing identifiers, while malformed frames and
notifications outside the business allowlist isolate the app-server process.
The JSONL reader also rejects duplicate keys at every nesting level,
NaN/Infinity, mixed or partial envelopes, non-local response ids, and unknown
top-level fields. Generated notifications may carry the exact optional
`emittedAtMs` envelope field; it is schema-validated but is not business
authority. Business strings are bounded before state: thread/turn ids at 512,
item/login ids at 256, and auth URLs at 2048 with the fixed OAuth/callback
host-and-path allowlist. Thread responses additionally bind cwd, ephemeral
mode, CLI version, and resume identity to the original request.
The exact server-notification inventory includes
`remoteControl/status/changed` and `account/login/completed`. Codex
0.149.0-alpha.4.1 emits the former before the first `account/read` response. It
is accepted only after full generated-envelope validation and then explicitly
dropped: DSH does not expose or interpret its server, installation, or
environment identity. The latter's generated `success=true` and
`success=false` forms settle a pending browser login. Cancellation instead
uses the generated `account/login/cancel` response—there are no invented
failed/canceled notification methods. A method-name-only probe of the exact
pinned binary observed no additional stable notifications during
`initialize`, `account/read`, `account/login/start`, and the cleanup
`account/login/cancel`; no account, credential, URL, path, or notification
payload is recorded by that test.
Validation errors are fixed categories and never include a raw frame, schema
path, working directory, prompt, or account payload. The bridge runtime pins
`jsonschema==4.26.0` in `bridge/requirements.txt`.

The generated stable vocabulary includes `process/exited`, whose
`ProcessExitedNotification` is explicitly the terminal for the client-owned
`process/spawn` method. Xiaoman does not call `process/spawn`, so that wire
notification is deliberately absent from `requiredWire.serverNotifications`
and the runtime business allowlist. Receiving even a generated-valid instance
is a protocol mismatch and isolates the App Server. It is never treated as
evidence that the App Server itself exited. App Server lifecycle authority uses
the internal-only `dsh/app-server/exited` sentinel, emitted only after the
owned OS process group has been verified gone; failed verification uses the
distinct `dsh/app-server/isolation-failed` sentinel.

Process lifecycle callbacks are generation-scoped and join one in-flight
termination result, so stdout EOF and the waiter cannot publish contradictory
authority. The stderr pipe is drained and discarded in fixed 16 KiB chunks;
its content is never logged, and a diagnostic without a newline cannot stop
the drain at the stream reader's line limit. Host interrupt/isolate controls
use bounded, extra-forbidden identities, and a supplied session/execution/
thread/turn tuple must identify one exact provider state before any side
effect. Bridge shutdown runs every owner cleanup stage and always reaches the
exact AppServer client close even when an earlier stage fails.
WS generator exhaustion is not Host release authority. Before emitting
`turn/released`, the bridge queries the provider's exact session/execution
ledger/event; only a true normal-completion or verified-isolation release is
accepted. A sticky `isolation_failed`/false ledger never emits that frame.
Bridge Codex timeout and queue settings are validated from their raw JSON
types before client construction; booleans, nulls, strings, non-finite values,
integral-float queue sizes, and out-of-range values cannot be coerced into a
spawnable configuration. A post-write ambiguous browser-login start/cancel
never retries: it boundedly isolates the current AppServer generation and
fails every pending login without retaining an auth URL. Pending login state
is exempt from TTL/LRU eviction, and a second start is rejected until the
owned flow reaches a terminal, cancel, or isolation fence.
Schema-invalid auth responses also join that exact generation's teardown
before releasing the auth operation lock; the reader's fixed protocol exit
hint remains authoritative. A reservation proven under the provider state
lock never to have entered dispatch is canceled atomically into a true exact
execution ledger, so a lost WS release can be acknowledged over HTTP without
killing an unrelated App Server. Terminal, poisoned, or possibly-dispatched
reservations retain ownership and cannot be erased into an unknown identity.
Pinned real-process coverage also verifies the empty-thread recovery grammar:
only `no rollout found for thread id <expected-id>` (with an optional terminal
period) rebuilds a mapping. Generic text, embedded diagnostics, and a foreign
thread id remain fail-closed; tests never record the real ids or payloads.
After an authoritative completed terminal has committed its durable mapping,
a later verified AppServer exit retires only that process generation and keeps
the mapping for exact restart/resume. Generated-valid `turn/completed` frames
for one thread/turn pair may repeat only when their params are semantically
identical (top-level `emittedAtMs` is non-authoritative); a contradiction
poisons the exact execution ledger, verifies process isolation, and removes
the mapping even when the first terminal was already cached.
