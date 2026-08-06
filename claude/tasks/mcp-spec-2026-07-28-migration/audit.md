# HTTP MCP Server vs MCP Spec 2026-07-28 — Migration Audit

**Date:** 2026-07-29 · **Audited:** `feat/http-mcp` (`packages/ai/src/ai/modules/mcp`, 23-tool surface)
**Spec:** [2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
**Verdict:** The architecture is already aligned — the server runs `StreamableHTTPSessionManager(stateless=True)`
and keys all cross-call state by explicit task tokens, which is exactly the pattern the new spec blesses
(SEP-2567). The migration is dominated by **one SDK bump** plus the **auth build-out**, which should now
target the new auth stack from the start. Almost nothing the spec deprecates is in use.

---

## Where we already comply (no work)

| Spec change | Our state | Evidence |
|---|---|---|
| Sessions removed (`Mcp-Session-Id` gone, SEP-2567) | Never used them: `stateless=True`, no session store anywhere | `modules/mcp/__init__.py:60-64` |
| Cross-call state via explicit server-minted handles | `TaskRegistry` + flow ring buffers keyed by task token; tokens returned by `run_pipeline` and passed back as tool args | `handlers.py` (task_registry), decisions log 2026-07-23 |
| Legacy HTTP+SSE transport deprecated | Not used — Streamable HTTP only (`json_response=False` = SSE-*response*-streaming per POST, which is Streamable HTTP, not the legacy transport). Legacy `rocketride-mcp-sse` lives only in the parked stdio package (`packages/client-mcp`) | `__init__.py:63`, `client-mcp/docs/index.md` |
| Roots / Sampling / Logging deprecated (SEP-2577) | None used. No `notifications/message`, no `logging/setLevel`, no roots, no sampling | `git grep` over `modules/mcp`: zero hits for elicit/sampling/roots/notifications |
| `ping`, `resources/subscribe`, list_changed notifications removed/replaced | None used — `monitor` is a bounded poll, `get_pipeline_trace` is a cursor drain, by design | `ingress-current-state.md` §4 |
| MRTR replaces server-initiated requests | We never send server-initiated requests | same grep |
| Elicitation `elicitationId` / `notifications/elicitation/complete` removed | Never used | same grep |
| Errors | Tool errors are normalized **in-band** (`{ok: false, error_type, ...}` in TextContent), not JSON-RPC error codes — unaffected by the error-code renumbering. No `-32002` usage | `handlers.py` `normalize_error` |

## Work items

### 1. SDK bump (the bulk of the mechanical work) — `mcp>=1.9.0` → 2026-07-28 release
`packages/ai/src/ai/requirements.txt` pins `mcp>=1.9.0` (a 2025-era floor). The Python SDK is Tier 1 and
ships 2026-07-28 support. The SDK carries most MUSTs for us:
- [ ] Bump the pin; expect **breaking constructor/API changes** in `StreamableHTTPSessionManager`
      (sessions are gone entirely, so the manager's session machinery — and possibly the class itself — changes;
      `stateless=` likely disappears as stateless becomes the only mode).
- [ ] `initialize`/`initialized` removal + per-request `_meta` (`protocolVersion`, `clientCapabilities`)
      parsing — SDK-provided; verify our lifespan wiring (`__init__.py:89-118`) still holds.
- [ ] `server/discover` (now **MUST**) — SDK-provided; verify it advertises our real capabilities
      (tools + resources, no prompts, `extensions: {}`).
- [ ] `resultType: "complete"` on all results — SDK-provided.
- [ ] `Mcp-Method`/`Mcp-Name` request headers (**required** on POSTs) + `HeaderMismatchError` (-32020) —
      SDK validates; verify our ASGI mount passes headers through untouched (`__init__.py:70-73`).
- [ ] Rewrite the in-process test harness: `test_mcp_module.py` uses `ClientSession` +
      `streamable_http_client` whose APIs change with the handshake removal. The boot-smoke
      (`test_register_all_yields_twenty_three_tools_total`) and live-verification patterns survive conceptually.

### 2. Cacheable lists (new REQUIRED fields — small real work)
- [ ] `tools/list`, `resources/list`, `resources/read` must return `ttlMs` + `cacheScope` (SEP-2549).
      Suggested: tools = long TTL (surface is static per build), `cacheScope: "private"` (future
      entitlement-filtered listing per node-auth workstream makes `"public"` unsafe);
      `rocketride://status` = short/zero TTL; `rocketride://pipelines` = short TTL (reflects running tasks).
- [ ] Deterministic tool order (SHOULD, prompt-cache hit rates): `registry.tools()` returns registration
      order from `register_all` — almost certainly already deterministic. **Verify + pin with a test.**

### 3. Auth (DEFERRED — Dylan, 2026-07-29; not part of phase 1)
Not needed for the migration or the phase-2 tracing work — it gates only OAuth-requiring host listings
(Claude.ai/ChatGPT connectors). Everything below is direction for **when** it gets built, not a phase-1
task. Phase 1 proceeds on the existing `authenticate_request` chain + dev bypass / per-user API keys.
The OAuth broker was already the known gap blocking Claude/ChatGPT listings (`http-auth-oauth-gap.md`);
today only the `authenticate_request` chain + dev bypass exists. Since nothing is built yet, build
against the 2026-07-28 stack from the start:
- [ ] Target **Client ID Metadata Documents (CIMD)** for client registration — **do not build DCR**
      (RFC 7591 is now deprecated; DCR only as optional back-compat if a partner AS requires it, with
      `application_type` handling per SEP-837).
- [ ] Authorization server: include `iss` in authorization responses (RFC 9207, SHOULD server-side;
      clients MUST validate it).
- [ ] Update `http-auth-oauth-gap.md` to reference the new spec's authorization section before any
      broker implementation starts.

### 4. Compatibility window strategy (decision, not code)
Servers on 2026-07-28 may not work with older clients and vice versa. Claude/Cursor/Claude.ai have not
shipped client support yet (spec is days old; 12-month deprecation window).
- [ ] Decide the gate: don't flip the production surface to a 2026-07-28-only SDK until the two or three
      hosts we target ship support (or the SDK proves dual-revision serving — check the SDK release notes
      for a compatibility mode when bumping).
- [ ] Re-run the `sse-surfacing-matrix` test kit against updated clients when they ship — it gates the
      whole live-trace-streaming question (see §6).

### 5. Optional adoptions (post-migration, opt-in wins)
- [ ] **Tasks extension** (`io.modelcontextprotocol/tasks`): our kick-off→poll shape (`run_pipeline` →
      token → `monitor`/`get_pipeline_trace`) is exactly its model. Adopting it makes long-running
      pipelines first-class for conforming clients (unsolicited task handles, `tasks/get` polling,
      `input_required` mid-flight). Candidate: `run_pipeline` returns a task handle when the client
      declares the extension.
- [ ] **`subscriptions/listen`**: the future live-trace/monitor feed (subscription state is
      request-scoped — fits our stateless engine; needs internal pub/sub fan-out). Gated on §4 client re-test.
- [ ] **OTel `traceparent` in `_meta`**: propagate into engine tasks so pipeline spans join the caller's
      trace — pairs with the PR #1612 `FlowSpanMapper` reuse discussion.
- [ ] **Gateway routing on `Mcp-Method`/`Mcp-Name`**: per-tool rate limiting/metering at the `ai/web`
      FastAPI layer without body parsing.

### 6. Docs (co-located docs rule)
- [ ] `modules/mcp/doc.md` — protocol version, header requirements, cache hints.
- [ ] `packages/client-mcp/docs/` — mark the SSE-mode server section deprecated-per-spec; note the stdio
      package's own migration is parked with it.

## Phase 2 — tracing revisit (after the migration lands)

Agreed sequencing (Dylan, 2026-07-29): implement this audit first, then do a dedicated pass over ALL
tracing options with the new primitives. The prior "streaming buys nothing" verdict
(`sse-surfacing-matrix.md`) was scoped to *mid-call rendering in 2025-era clients* — it does not cover
these, in rough order of how little they depend on client behavior:

1. **Client-independent (testable immediately after migration, no waiting on hosts):**
   - `_meta` `traceparent` propagation → engine task → OTel spans (pairs with PR #1612's
     `FlowSpanMapper` behind an in-memory processor). Delivers client-correlated tracing through any
     OTel backend with zero client cooperation.
   - `get_pipeline_trace` serving OTel-semconv span trees (mapper reuse) — richer payload, same
     pull-drain shape.
2. **Client-capability-gated but pull-based (works with any conforming 2026-07-28 client):**
   - Tasks extension: `run_pipeline` → unsolicited task handle → `tasks/get` polling with status
     messages carrying per-node progress. Conforming clients poll and CAN show status between polls —
     different rendering path than the one that failed in the matrix.
3. **The re-test (the only part that repeats the old experiment):**
   - `subscriptions/listen` feed of flow events. Structurally immune to the old failure (no pending
     call result to buffer against), but rendering is still host UX. Re-run the surfacing test kit per
     client as they ship spec support; record results in a new matrix beside the old one.

Test plan skeleton: extend `claude/tasks/http-mcp-ingress/surfacing-test-kit.md` — same probe server,
three new scenarios (task-status polling visibility, subscription-stream visibility, traceparent
round-trip), one row per client per scenario.

## Explicitly NOT needed
- No session-store teardown (never had one) · No handshake-removal surgery in our code (SDK's job) ·
  No MRTR work (we never initiate server→client requests) · No notification migration (we emit none) ·
  No error-code renumbering (errors are in-band JSON).
