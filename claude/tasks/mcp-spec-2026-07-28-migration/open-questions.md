# Open Questions — MCP 2026-07-28 Migration

_As of 2026-07-29._

1. **Python SDK release shape** — which `mcp` version implements 2026-07-28, and does it offer a
   dual-revision compatibility mode (serve old + new clients during the 12-month window)? Determines
   the timing gate in audit §4. Not yet checked against PyPI/release notes.
2. **`StreamableHTTPSessionManager` API fate** — assumed breaking changes; the exact replacement API is
   unverified until the SDK bump is attempted.
3. **Tool-order determinism** — `registry.tools()` is believed insertion-ordered via `register_all`;
   unverified (Bash access was flaky during the audit). Needs a pinning test.
4. **`cacheScope` for `tools/list`** — `"private"` recommended because the node-auth workstream plans
   entitlement-filtered listings; if that lands differently, `"public"` + long TTL is a better cache win.
   Owner call: Dylan + Charlie (node auth).
5. **Which hosts gate the flip** — Claude.ai connectors, Claude Code, Cursor? Product call on which
   client adoptions must land before the surface moves to the new revision.
6. **stdio package (`packages/client-mcp`)** — parked/OSS lane; does it migrate at all, and when? Its
   legacy SSE server mode is now formally Deprecated upstream.
7. **Tasks-extension adoption** — worth doing in the migration PR or as a follow-up? (Audit recommends
   follow-up.)
8. ~~**crewai constraint conflict**~~ — RESOLVED 2026-07-30: `depends.py` now supports uv
   `--override` files (`OVERRIDES_GLOBS`); `packages/ai/src/ai/overrides.txt` forces
   `mcp>=2.0.0,<3` past crewai's `<1.29` cap. Safe: agent_crewai never touches crewai's
   MCP features (lazy import, verified). Remove when crewai ships mcp-2 support.
