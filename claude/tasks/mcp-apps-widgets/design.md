# MCP Apps Widgets — Design (approved 2026-07-30)

Embedded UI (MCP Apps, extension `io.modelcontextprotocol/ui`, spec `2026-01-26`)
for the RocketRide HTTP MCP server. Program-level design; the executable plan for
slice 1 is `plan.md` in this directory. Research backing:
`claude/research/connectors-project/embedded-ui/`.

## Decisions (Dylan, 2026-07-29/30)

- **Goal**: product surface for users — breadth of daily-driver widgets, not a
  one-off demo.
- **Scope**: all four widgets — pipelines table, run monitor/trace, pipeline
  graph, dropper.
- **Hosts**: build once to the MCP Apps standard (works in Claude, ChatGPT,
  Goose, Cursor, VS Code); actively verify in Claude; keep bundles
  ChatGPT-clean (`structuredContent`, no host-specific APIs).
- **Auth**: no OAuth dependency. Dev/test via MCPJam Inspector (no tunnel) and
  cloudflared tunnel + unlisted Claude custom connector with `MCP_DEV_NO_AUTH=1`.
  The OAuth broker matters only for directory listing — out of scope here.
- **Branch**: all of it lands on `feat/http-mcp` (2026-07-30 amendment; the
  earlier "stack feat/mcp-apps" choice is superseded). Reason: the prerequisite
  depends-override patch belongs to the migration work already on that branch.
- **Prerequisite folded in**: the mcp-v2 pin (`mcp>=2.0.0,<3`) is unsatisfiable
  with crewai's `mcp<1.29` cap, breaking the engine's combined dependency solve.
  Fix = uv `--override` support in `depends.py` (proven empirically: compile and
  install both resolve with an override file; both fail without). Safe because
  `agent_crewai` never uses crewai's MCP features and crewai imports `mcp`
  lazily. Remove the override when crewai ships mcp-2 support.

## Architecture

- **Widget workspace** `apps/mcp-widgets/`: one Vite project, one entry per
  widget, each bundled by `vite-plugin-singlefile` into a self-contained HTML
  file; `@modelcontextprotocol/ext-apps` for the postMessage bridge. Output is
  copied to `packages/ai/src/ai/modules/mcp/apps/dist/` (gitignored), which the
  existing `ai:build` syncDir carries into the server dist.
- **Server side** (`packages/ai/src/ai/modules/mcp/`): new `apps.py` declares
  `AppSpec`s (uri, filename, title), serves `ui://rocketride/*.html` resources
  with mimeType `text/html;profile=mcp-app`, and exposes the extension
  capability. `ToolRegistry.register` gains `ui_resource_uri=`; `tools()` emits
  `_meta.ui.resourceUri` (SDK v2 `Tool.meta` serializes to `_meta` — verified).
  `Server.extensions` (SDK v2 first-class attr) advertises
  `{'io.modelcontextprotocol/ui': {'mimeTypes': [...]}}` when widgets exist.
  Hosts without the extension see today's JSON, unchanged.
- **Data flow**: widgets call existing tools via the bridge (`callServerTool`);
  no new network paths except the dropper's browser `fetch` to the
  `pk_`-tokenized upload URL (declared in that widget's `csp.connectDomains`).

## Rollout (what / when / how surfaced)

| Slice | Widget | Linked tools | Plan |
|---|---|---|---|
| 1 | Plumbing + **pipelines table** | `list_running_pipelines` | `plan.md` (this dir) — includes the depends-override prerequisite |
| 2 | **Dropper** (flagship) | `run_dropper_pipe` | separate plan, after slice 1 lands; ports dropper-ui Views |
| 3 | **Run monitor / trace** | `monitor`, `get_pipeline_trace` | separate plan; polls trace via `since` cursor |
| 4 | **Pipeline graph** | `describe_pipeline`, `validate_pipeline` | separate plan |

Each slice is independently demoable (MCPJam locally; Claude via tunnel).
Slices 2–4 get their own plan docs once slice 1's verified bridge behavior
informs them — their widget internals are deliberately not designed here.

## Testing

- Python contract tests (`packages/ai/tests/ai/modules/mcp/test_apps.py`) using
  the existing SDK v2 in-memory `Client(server)` harness: capability
  advertisement, `ui://` resource listing/serving, `_meta.ui` on linked tools,
  graceful absence when no widget bundle exists.
- Widget build: `tsc --noEmit` + Vite build in `mcp-widgets:build`.
- Manual: MCPJam Inspector runbook per slice; Claude custom-connector runbook
  for host verification.
