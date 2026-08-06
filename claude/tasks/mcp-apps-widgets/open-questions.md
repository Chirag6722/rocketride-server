# MCP Apps Widgets — Open Questions

_As of 2026-07-30 (second pass: resolved against the codebase/registry/web where possible)._

## Resolved

1. ~~**`@modelcontextprotocol/ext-apps` version pin**~~ — RESOLVED: npm latest is **1.7.5**;
   pinned `^1.7.0` in the plan. API verified against the package's `app.d.ts`: `App`,
   `connect`, `ontoolresult`, `callServerTool` (plus `sendMessage`, `requestDisplayMode`,
   `onhostcontextchanged` for later slices). Exports include `./react` and `*-with-deps`
   variants.
2. ~~**`list_running_pipelines` result contract**~~ — RESOLVED from `tools/visibility.py`:
   returns `{'ok': True, 'tasks': running, 'count': len(running)}`; rows come from the
   engine's `rrext_get_tasks` and the module's contract fixture shows fields
   `{'name', 'description', 'token'}` (no `state` in the row — per-task state needs
   `get_task_status`, deferred past slice 1). `terminate` requires `task_token`.
   Plan's widget code updated to match.
3. ~~**Dropper `connectDomains` templating (mechanism half)**~~ — RESOLVED: the engine
   client already exposes `client.base_url` (used by `run_dropper_pipe` to mint
   `upload_url`/`dropper_url`, execution.py:227), and `build_mcp_server` holds
   `engine_factory` — so `apps.py` can stamp `_meta.ui.csp.connectDomains` with the
   engine origin at resource-serve time. Remaining for slice 2 is only the *policy*
   choice: serve-time CSP vs host file-upload APIs (ChatGPT `uploadFile`; the standard's
   file story is thinner).
4. ~~**Claude custom-connector auth for in-host testing**~~ — RESOLVED (web, 2026-07-30):
   - **No-auth custom connectors are supported** (auth type `none`,
     claude.com/docs/connectors/building/authentication) — a bare URL works on
     Free/Pro/Max.
   - **Static header auth (`static_headers`, e.g. Authorization/x-api-key) exists but is
     BETA**, slow rollout, org-admin framed — don't plan on it being available.
   - Connections originate from **Anthropic's servers** (160.79.104.0/21), even for
     Desktop — the tunnel must be publicly reachable. Consequence: `MCP_DEV_NO_AUTH=1`
     over a tunnel exposes an unauthenticated engine to the internet; keep tunnel
     sessions short-lived and tear down after each verification run.
   - Custom connectors **do render MCP Apps** in practice (the directory-submission
     workflow itself tests widgets via custom connector first), but it's not explicitly
     documented and there's an open rendering bug (ext-apps#671) — MCPJam stays the
     primary verification, Claude the secondary.
   - Claude Code allows arbitrary headers via `claude mcp add` but is a terminal — no
     widget rendering.
6. ~~**Engine start for the MCPJam runbook**~~ — RESOLVED from the migration plan's
   phase-1 live verification: local dist engine, WS on :5565, `/mcp` on the engine web
   server; env `MCP_DEV_NO_AUTH=1`, `ROCKETRIDE_URI=http://localhost:5565`,
   `ROCKETRIDE_AUTH=MYAPIKEY`; codesign-SIGKILL workaround in memory
   `engine_codesign_sigkill`. Runbook updated. Bonus: the stale
   `dist/server/cache/constraints.txt` gotcha from the migration review self-heals in
   Task 1 (the requirements hash changes, forcing recompile).
7. ~~**`structuredContent` for widget-linked tools**~~ — DECIDED: slice 2, implemented
   centrally in `_on_call_tool` (every handler already returns a JSON dict; one line adds
   `structuredContent=result` to `CallToolResult` — no per-tool work). Not needed for
   MCPJam/Claude verification in slice 1.

## Still open

5. **crewai override removal** — crewai `1.15.9` (checked 2026-07-30) still caps
   `mcp>=1.28.1,<1.29`. Watch upstream for an mcp>=2 release; then delete the entry in
   `packages/ai/src/ai/overrides.txt` and the migration-doc note. Consider a CI check
   that flags the override as obsolete once resolution succeeds without it.

## Opened by the slice 2–4 plans (2026-08-03)

8. **Dropper upload duration vs browser limits** — the `/task/data` POST is synchronous
   (blocks until the pipeline finishes; verified in task_data.py). Long pipelines hold
   the XHR open; browsers/hosts may time out multi-minute requests. Measure on a real
   slow pipe during the slice-2 manual pass; if it breaks, fall back to
   monitor/get_pipeline_trace polling after upload instead of relying on the POST body.
9. ~~**Widget-initiated tool-call consent UX per host**~~ — MOOT for the run-monitor
   (widget removed 2026-08-05, Dylan's call after live testing). Still relevant in
   principle for any future widget that polls via the bridge; the pipelines-table's
   one-shot refresh/terminate calls are the only bridge calls left.
10. ~~**Pipe-result media field names**~~ — RESOLVED (2026-08-06, live pass +
    source): the widget's original assumption (`mime_type` + base64 `data`) was
    wrong twice over. (a) The POST response is the standard engine envelope
    `{status, data}` (fixed in `9d4a001d`). (b) Media entries are shaped
    `{mime_type, <lane>}` with the base64 payload under the lane name
    (`image`/`audio`/`video`), per dropper-ui's `processMediaData` and
    `task_data.py` — and field→type routing comes from `resultTypes`. The
    widget now mirrors dropper-ui's tabbed presentation (`c534685b`).
11. ~~**`.pipe` `input` edge encoding**~~ — CLOSED, SUPERSEDED (2026-08-05): slice-4
    Task 1 derived the encoding (array of `{lane, from}` objects) from
    `pipeline.ts` + `examples/*.pipe`; evidence was recorded in
    `apps/mcp-widgets/src/pipeline-graph/edges.test.md`. That file and the
    `edges.ts` parser it documented were both deleted in slice 5 along with the
    rest of the pipeline-graph widget (replaced by the real-canvas iframe
    shell), so there is no client-side edge parser left to keep correct here.
    The encoding evidence is preserved in git history at commit `5acff373` (and
    summarized in the runbook's slice-4 section above) if a future client-side
    parser is ever reintroduced.

## Slice-3 deferred follow-ups (2026-08-03, from final review — fold into slice 4 or a polish pass)

12. ~~**run-monitor longevity minors**~~ — MOOT (widget removed 2026-08-05).

**12b. Shared `parsePayload` library extraction** — downgraded again after the
canvas removal (2026-08-05): only two widgets remain (`dropper`'s `parseInfo`,
`pipelines-table`'s `parseRows`). At two call sites the extraction is marginal —
do it opportunistically if a third widget ever appears, not as standalone work.

## Opened by the slice 5 plan (2026-08-05)

13. ~~**`frameDomains` host-support matrix**~~ — MOOT (2026-08-05): the entire
    canvas surface (pipeline-canvas widget, /canvas module, canvas_stash,
    apps/canvas-ui) was removed after Dylan's manual pass showed the embedded
    render nowhere near the real canvas (likely: no auto-layout for
    position-less inline pipelines — see the runbook's slice-5 removal note).
    No widget declares frameDomains anymore; the AppSpec flag was removed too.
    Implementation preserved at commit 5731b656 if ever revisited.
