# MCPJam widget verification

1. Build: `./builder mcp-widgets:build && ./builder ai:build`
2. Start the local dist engine with the MCP module and auth bypass —
   same setup as the migration's phase-1 live verification (engine WS on
   :5565, `/mcp` mounted on the engine web server): env `MCP_DEV_NO_AUTH=1`,
   `ROCKETRIDE_URI=http://localhost:5565`, `ROCKETRIDE_AUTH=MYAPIKEY`.
   If the dist engine SIGKILLs at boot (codesign), apply the workaround in
   memory note `engine_codesign_sigkill` (`codesign --force -s -`).
3. `npx @mcpjam/inspector` → connect to `http://localhost:<engine web port>/mcp`
   (Streamable HTTP).
4. Check initialize response advertises `extensions['io.modelcontextprotocol/ui']`.
5. Run `list_running_pipelines` → the pipelines-table widget must render
   (empty-state "No pipelines running." is a pass if nothing runs).
6. Start any pipeline (`run_pipeline`), re-run the tool → row appears;
   click Terminate → row disappears after refresh.

Record results (pass/fail + screenshots) below.

## Automated pre-checks (2026-08-03)

Run non-interactively (no browser available in this environment). These checks
build the real MCP server object in-process — exercising the actual `apps.py`
resource-serving code, the actual built widget bundle on disk, and the actual
`ToolRegistry` `_meta.ui` wiring — over the `mcp.client.Client` in-memory
transport, mirroring the fixture patterns in
`packages/ai/tests/ai/modules/mcp/test_apps.py`. This does **not** replace
steps 2–3 (real engine boot, real Streamable HTTP transport, real MCPJam
browser rendering) — see "Manual execution" below for what's still open.

**Environment:** fast venv python at
`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcpv2/bin/python`
(pre-provisioned with `mcp==2.0.0` and a `depends` stub in site-packages), run
from `packages/ai` with `PYTHONPATH=src` so `ai.*` imports resolve to source.
Server built via `ai.modules.mcp.handlers.build_mcp_server` with the
**default** `apps_dir` (no override — reads the real
`packages/ai/src/ai/modules/mcp/apps/dist/pipelines-table.html`, 338589 bytes
on disk at run time) and a stub `EngineClient` (`list_tasks`/`deploy_list`
return `[]`, enough for `list_running_pipelines` to execute without a live
engine).

Script (`/private/tmp/.../scratchpad/mcp_apps_smoke.py`):

```python
"""In-process smoke test for the MCP Apps embedded-UI surface.

Builds the real server (default apps_dir, i.e. apps/dist next to the mcp
module -- NOT a tmp_path override) with a stub engine factory, and drives it
over the in-memory mcp.client.Client the way tests/ai/modules/mcp/test_apps.py
does. Verifies:

  1. initialize capabilities advertise the io.modelcontextprotocol/ui extension
  2. list_resources includes the ui:// pipelines-table resource
  3. read_resource returns the REAL built bundle (len > 100_000, contains '<script')
  4. list_tools shows _meta.ui.resourceUri on list_running_pipelines

Run with the fast venv python from packages/ai (so `ai` imports resolve):
  cd packages/ai && PYTHONPATH=src <venv>/bin/python /path/to/mcp_apps_smoke.py
"""

import asyncio
import sys

from mcp.client import Client

from ai.modules.mcp import apps, handlers


class StubEngineClient:
    """Minimal EngineClient stub -- enough for list_running_pipelines to run."""

    async def list_tasks(self):
        return []

    async def deploy_list(self):
        return []


async def main() -> int:
    failures = []

    def check(label, condition, detail=''):
        status = 'PASS' if condition else 'FAIL'
        print(f'[{status}] {label}' + (f' -- {detail}' if detail and not condition else ''))
        if not condition:
            failures.append(label)

    server = handlers.build_mcp_server(lambda: StubEngineClient())

    check(
        'server declares io.modelcontextprotocol/ui extension',
        apps.UI_EXTENSION_ID in server.extensions
        and server.extensions[apps.UI_EXTENSION_ID] == {'mimeTypes': [apps.UI_MIME_TYPE]},
        detail=repr(server.extensions),
    )

    async with Client(server) as client:
        capabilities = client.server_capabilities
        raw_caps = capabilities.model_dump(by_alias=True, exclude_none=True) if capabilities else {}
        extensions = raw_caps.get('extensions', {})
        check(
            'initialize response capabilities carry the ui extension',
            apps.UI_EXTENSION_ID in extensions,
            detail=repr(raw_caps),
        )

        listed = await client.list_resources()
        uris = [str(r.uri) for r in listed.resources]
        check(
            'list_resources includes ui:// pipelines-table resource',
            apps.PIPELINES_TABLE_URI in uris,
            detail=repr(uris),
        )

        read = await client.read_resource(apps.PIPELINES_TABLE_URI)
        text = read.contents[0].text if read.contents else ''
        check('bundle length > 100_000 chars', len(text) > 100_000, detail=f'len={len(text)}')
        check("bundle contains '<script'", '<script' in text)

        tools = await client.list_tools()
        tool = next((t for t in tools.tools if t.name == 'list_running_pipelines'), None)
        check('list_running_pipelines tool present', tool is not None)
        if tool is not None:
            check(
                'list_running_pipelines carries _meta.ui.resourceUri',
                tool.meta == {'ui': {'resourceUri': apps.PIPELINES_TABLE_URI}},
                detail=repr(tool.meta),
            )

    print()
    if failures:
        print(f'{len(failures)} check(s) FAILED: {failures}')
        return 1
    print('All checks PASSED.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
```

Result:

```
[PASS] server declares io.modelcontextprotocol/ui extension
[PASS] initialize response capabilities carry the ui extension
[PASS] list_resources includes ui:// pipelines-table resource
[PASS] bundle length > 100_000 chars
[PASS] bundle contains '<script'
[PASS] list_running_pipelines tool present
[PASS] list_running_pipelines carries _meta.ui.resourceUri

All checks PASSED.
```

Verdict: **PASS** for everything the automated harness can reach — the
extension is correctly advertised (both on the `Server` object and through a
real `initialize` handshake), the widget resource lists and reads back as the
real built HTML bundle, and `list_running_pipelines` correctly carries
`_meta.ui.resourceUri`. This also caught and fixed a real regression: running
the full `./builder ai:test` suite with the built widget bundle present on
disk broke a pre-existing test
(`test_handlers.py::test_list_resources_returns_exactly_status_and_pipelines`)
that asserted resource listing returned *exactly* the two JSON resources
without isolating itself from `apps/dist` state — fixed by passing an empty
`tmp_path` as `apps_dir` in that test (it's about the JSON resource surface,
not the widget surface, which is covered separately in `test_apps.py`).

## Manual execution — results (Dylan, 2026-08-03)

Executed against the local dist engine from the main checkout (branch tip
`6624af2d`), MCPJam Inspector at `127.0.0.1`, RocketRide server connected with
23 tools listed.

- [x] Step 2: dist engine booted, `/mcp` reachable (engine binary needed the
      known `codesign --force -s -` re-sign after the branch switch)
- [x] Step 3: MCPJam connected over Streamable HTTP
- [x] Step 4: real-wire verification — MCPJam's `resources/read` of
      `ui://rocketride/pipelines-table.html` returned the full bundle with
      `mimeType: text/html;profile=mcp-app` and `cacheScope: private`
      (connect-log capture); host proceeded to widget rendering, implying the
      extension capability was honored
- [x] Step 5: `list_running_pipelines` rendered the pipelines-table widget
      inline in MCPJam; empty state "No pipelines running." displayed
      correctly (screenshot captured in session)
- [x] Step 6 (partial): started a dropper pipe → re-ran the tool → row
      rendered with Name `dropper_1`, Description `RocketRide DTC MCP Tool`,
      Token `tk_76cf...a3c53`, with Terminate and Refresh buttons present.
      MCPJam's Inline / PiP / Fullscreen display-mode controls active on the
      widget.
- [ ] Step 6 (remaining): Terminate click-through (row disappears after
      refresh) not yet observed/recorded — note the deferred minor: the
      Terminate handler has no error catch, so a failed call leaves the
      button disabled.

Observations for follow-up:
- `serverInfo.version` is an empty string on the wire (we construct the
  low-level `Server` with a name but no version) — cosmetic; pass the ai
  package version when convenient.

Verdict: slice-1 widget **renders and behaves in a real MCP Apps host**.

## Slice 2 — dropper (pending manual execution by Dylan)

1. Build + boot as in steps 1–3 above (widget bundle now includes `dropper`
   as well as `pipelines-table`; both are built by
   `./builder mcp-widgets:build`).
2. In MCPJam, call `run_dropper_pipe` against a simple pipeline (any pipe
   that accepts a file input and returns objects is fine for this pass).
   The dropper widget must render a dropzone.
3. Drop a small text file onto the dropzone → upload progress indicator →
   status flips to "Processing…" → pipeline results render in the widget
   (per the documented dropper-ui conventions: `mime_type` + base64 `data`
   or `text` fields on `objects`, JSON fallback otherwise — see Task 6
   brief "known risks" if a live sample uses different field names).
4. **Synchronous upload note:** the widget's POST to the engine is
   synchronous — a long-running pipeline holds the HTTP request open for
   its full duration, so ordinary browser/host request timeouts apply.
   Flag it here if a real pipe run exceeds roughly 5 minutes; that's the
   threshold where a follow-up (progress polling / chunked upload) becomes
   worth scoping.
5. **CSP note:** the dropzone's direct upload to the engine origin is only
   authorized because the `dropper` resource's `_meta.ui.csp.connectDomains`
   is stamped with the live engine origin at list time (see doc.md,
   "Embedded UI (MCP Apps)"). MCPJam does not enforce this — it will let
   the upload through regardless. Claude enforces the declared domain list
   strictly, so a real Claude host is the one place this can still fail
   silently; if the upload XHR is blocked there, capture the console error
   verbatim (per the Task 6 brief's known risks) as the key datum for a
   follow-up.

Record results (pass/fail + screenshots) below, following the same format
as the slice-1 "Manual execution" section above.

### Manual execution — Dylan, first live pass (recorded 2026-08-05)

- Steps 1–3 partial PASS: widget rendered the dropzone, upload + progress
  worked, and the pipeline received the data. FAIL at render: widget showed
  `undefined/undefined objects processed` and no result objects.
- Root cause (fixed in `9d4a001d`): the `/task/data` POST returns the
  standard engine envelope `{status: 'OK', data: DataResult}` (see
  `ai/web/response.py`), not a bare `DataResult`. The widget read the
  counts/objects at the top level. Fix unwraps the envelope and surfaces
  `{status: 'Error'}` responses as a visible pipeline error.
- Still unconfirmed after the fix: open-questions #10 (media field names
  inside `objects`) — needs a retest where results actually render.

### Automated pre-checks (2026-08-03)

Extends the slice-1 automated pre-check pattern with the dropper-specific
assertions: the CSP stamping on the resource listing, the dropper bundle
read-back, the `run_dropper_pipe` meta link, and `structured_content` on a
tool call result. Same caveats as slice-1 — this is the in-process
`mcp.client.Client` harness, not a real engine boot or real browser
rendering; it does not replace steps 1–4 above.

**Environment:** same fast venv python as the slice-1 pre-checks
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcpv2/bin/python`),
run from `packages/ai` with `PYTHONPATH=src`. Server built via
`ai.modules.mcp.handlers.build_mcp_server` with the **default** `apps_dir`
(reads the real `packages/ai/src/ai/modules/mcp/apps/dist/dropper.html`,
340,727 bytes on disk at run time) and a stub `EngineClient` that adds a
`base_url` property returning `http://localhost:5565` — this is what feeds
`_on_list_resources`' CSP-stamping path (`handlers.py`: `engine_origin =
engine_factory().base_url`), so the dropper resource's `_meta.ui.csp` gets
stamped exactly as it would with a live engine.

Script
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcp_apps_dropper_smoke.py`):

```python
"""In-process smoke test for the slice-2 dropper widget (MCP Apps).

Builds the real server (default apps_dir, i.e. apps/dist next to the mcp
module -- NOT a tmp_path override) with a stub engine factory that also
exposes `base_url` (so the CSP-stamping path in
`handlers._on_list_resources` has an engine origin to stamp with), and
drives it over the in-memory mcp.client.Client the way
tests/ai/modules/mcp/test_apps.py does. Verifies:

  1. list_resources includes the ui:// dropper resource, with
     `_meta.ui.csp.connectDomains == ['http://localhost:5565']`
  2. read_resource returns the REAL built dropper bundle (len > 10_000
     chars, contains '<script')
  3. list_tools shows `_meta.ui.resourceUri` == DROPPER_URI on
     run_dropper_pipe
  4. calling list_running_pipelines returns a CallToolResult carrying
     `structured_content`

Run with the fast venv python from packages/ai (so `ai` imports resolve):
  cd packages/ai && PYTHONPATH=src <venv>/bin/python /path/to/mcp_apps_dropper_smoke.py
"""

import asyncio
import sys

from mcp.client import Client

from ai.modules.mcp import apps, handlers

ENGINE_ORIGIN = 'http://localhost:5565'


class StubEngineClient:
    """Minimal EngineClient stub -- enough for list_running_pipelines to run,
    plus a `base_url` property so the CSP-stamping path has an origin."""

    @property
    def base_url(self):
        return ENGINE_ORIGIN

    async def list_tasks(self):
        return []

    async def deploy_list(self):
        return []


async def main() -> int:
    failures = []

    def check(label, condition, detail=''):
        status = 'PASS' if condition else 'FAIL'
        print(f'[{status}] {label}' + (f' -- {detail}' if detail and not condition else ''))
        if not condition:
            failures.append(label)

    server = handlers.build_mcp_server(lambda: StubEngineClient())

    async with Client(server) as client:
        listed = await client.list_resources()
        dropper = next((r for r in listed.resources if str(r.uri) == apps.DROPPER_URI), None)
        check('list_resources includes ui:// dropper resource', dropper is not None,
              detail=repr([str(r.uri) for r in listed.resources]))
        if dropper is not None:
            check(
                'dropper resource carries _meta.ui.csp.connectDomains == [engine origin]',
                dropper.meta == {'ui': {'csp': {'connectDomains': [ENGINE_ORIGIN]}}},
                detail=repr(dropper.meta),
            )

        read = await client.read_resource(apps.DROPPER_URI)
        text = read.contents[0].text if read.contents else ''
        check('dropper bundle length > 10_000 chars', len(text) > 10_000, detail=f'len={len(text)}')
        check("dropper bundle contains '<script'", '<script' in text)

        tools = await client.list_tools()
        tool = next((t for t in tools.tools if t.name == 'run_dropper_pipe'), None)
        check('run_dropper_pipe tool present', tool is not None)
        if tool is not None:
            check(
                'run_dropper_pipe carries _meta.ui.resourceUri == DROPPER_URI',
                tool.meta == {'ui': {'resourceUri': apps.DROPPER_URI}},
                detail=repr(tool.meta),
            )

        result = await client.call_tool('list_running_pipelines', {})
        check(
            'list_running_pipelines call result carries structured_content',
            result.structured_content is not None,
            detail=repr(result.structured_content),
        )

    print()
    if failures:
        print(f'{len(failures)} check(s) FAILED: {failures}')
        return 1
    print('All checks PASSED.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
```

Result:

```
[PASS] list_resources includes ui:// dropper resource
[PASS] dropper resource carries _meta.ui.csp.connectDomains == [engine origin]
[PASS] dropper bundle length > 10_000 chars
[PASS] dropper bundle contains '<script'
[PASS] run_dropper_pipe tool present
[PASS] run_dropper_pipe carries _meta.ui.resourceUri == DROPPER_URI
[PASS] list_running_pipelines call result carries structured_content

All checks PASSED.
```

Verdict: **PASS** for everything the automated harness can reach — the
dropper resource is stamped with the live engine origin's CSP
`connectDomains`, the widget resource lists and reads back as the real
built HTML bundle, `run_dropper_pipe` correctly carries
`_meta.ui.resourceUri`, and tool call results carry `structured_content`
end-to-end through the client. Steps 1–5 above (real engine boot, real
Streamable HTTP transport, real MCPJam/Claude browser rendering of the
dropzone, and the upload/progress/results UX) remain **pending manual
execution by Dylan**.

## Slice 3 — run monitor (WIDGET REMOVED 2026-08-05 — section retained for history)

> Dylan's decision after first live testing: the run-monitor UI is not needed.
> The widget was removed (source, registration, tool links, test); `monitor`
> and `get_pipeline_trace` remain plain JSON tools. Steps below are obsolete.

1. Build + boot as in steps 1–3 above (widget bundle now includes
   `run-monitor` as well as `pipelines-table`/`dropper`; all three are built
   by `./builder mcp-widgets:build`).
2. In MCPJam, call `run_pipeline` against any pipe, with
   `pipelineTraceLevel: 'full'` set on the call — this flow-subscribes the
   task so `get_pipeline_trace` has events to drain (see doc.md, "Per-node
   tracing"). **A task started without `pipelineTraceLevel` produces no flow
   events at all, by design** — if the feed stays empty, check this first
   before treating it as a bug.
3. Call `monitor` against the returned `task_token`. The run-monitor widget
   must render: a header with a status chip (state label) and the
   completed/failed/total counts from the pushed snapshot, then start
   polling `get_pipeline_trace` (paging forward via `since`) and `monitor`
   itself through the bridge, roughly every few seconds (2.5s idle cadence;
   the bundled `monitor` call can stretch a cycle to ~7.5s while a task is
   actively running, since it blocks up to its own 5s timeout). As the
   pipeline processes data, the feed should fill with per-node trace
   events; the status chip should update on each poll.
4. Let the task run to completion (or terminate it) and confirm the poll
   loop **stops** once `monitor`'s snapshot reports a terminal state — no
   further `callServerTool` calls after that point (watch the host's
   MCP traffic log / dev tools network panel).
5. **Consent-prompt note:** each poll tick is a widget-initiated
   `callServerTool` back through the bridge (two calls per tick: `monitor`
   + `get_pipeline_trace`). Host consent UX for widget-initiated tool calls
   differs — record exactly how MCPJam behaves here (expected: silent,
   no per-call prompt). If tested against Claude, expect it may prompt more
   aggressively (per call or per session); this is expected consent
   behavior, not a bug, but if it prompts on every poll tick (roughly every
   few seconds) that's a finding for open question #9 in `open-questions.md` (mitigation
   candidates — longer interval, manual refresh button — are a follow-up,
   not a fix to make here).

Record results (pass/fail + screenshots) below, following the same format
as the slice-1 "Manual execution" section above.

### Automated pre-checks (2026-08-03)

Extends the slice-1/slice-2 automated pre-check pattern with the
run-monitor-specific assertions: the resource listing (with no CSP meta,
since run-monitor makes no direct network calls of its own — all data
arrives via `callServerTool` through the bridge), the widget bundle
read-back, and the `_meta.ui.resourceUri` link on **both** tools the widget
is registered against (`monitor` and `get_pipeline_trace`). Same caveats as
slice-1/slice-2 — this is the in-process `mcp.client.Client` harness, not a
real engine boot or real browser rendering; it does not replace steps 1–4
above.

**Environment:** same fast venv python as the slice-1/slice-2 pre-checks
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcpv2/bin/python`),
run from `packages/ai` with `PYTHONPATH=src`. Server built via
`ai.modules.mcp.handlers.build_mcp_server` with the **default** `apps_dir`
(reads the real
`packages/ai/src/ai/modules/mcp/apps/dist/run-monitor.html`, 340,542 bytes
on disk at run time) and a stub `EngineClient` (`list_tasks`/`deploy_list`
return `[]` — enough for the server to build; `monitor`/`get_pipeline_trace`
are only inspected via `list_tools`/`list_resources` here, not called).

Script
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcp_apps_run_monitor_smoke.py`):

```python
"""In-process smoke test for the slice-3 run-monitor widget (MCP Apps).

Builds the real server (default apps_dir, i.e. apps/dist next to the mcp
module -- NOT a tmp_path override) with a stub engine factory, and drives it
over the in-memory mcp.client.Client the way tests/ai/modules/mcp/test_apps.py
does. Verifies:

  1. list_resources includes the ui:// run-monitor resource, with NO
     `_meta.ui.csp` (run-monitor makes no direct network calls -- all data
     arrives via callServerTool through the bridge, unlike dropper)
  2. read_resource returns the REAL built run-monitor bundle (len > 10_000
     chars, contains '<script')
  3. list_tools shows `_meta.ui.resourceUri` == RUN_MONITOR_URI on BOTH
     `monitor` and `get_pipeline_trace`

Run with the fast venv python from packages/ai (so `ai` imports resolve):
  cd packages/ai && PYTHONPATH=src <venv>/bin/python /path/to/mcp_apps_run_monitor_smoke.py
"""

import asyncio
import sys

from mcp.client import Client

from ai.modules.mcp import apps, handlers


class StubEngineClient:
    """Minimal EngineClient stub -- enough for the server to build and
    list_tools/list_resources/read_resource to run without a live engine."""

    async def list_tasks(self):
        return []

    async def deploy_list(self):
        return []


async def main() -> int:
    failures = []

    def check(label, condition, detail=''):
        status = 'PASS' if condition else 'FAIL'
        print(f'[{status}] {label}' + (f' -- {detail}' if detail and not condition else ''))
        if not condition:
            failures.append(label)

    server = handlers.build_mcp_server(lambda: StubEngineClient())

    async with Client(server) as client:
        listed = await client.list_resources()
        run_monitor = next((r for r in listed.resources if str(r.uri) == apps.RUN_MONITOR_URI), None)
        check('list_resources includes ui:// run-monitor resource', run_monitor is not None,
              detail=repr([str(r.uri) for r in listed.resources]))
        if run_monitor is not None:
            check(
                'run-monitor resource carries no _meta.ui.csp (no direct network calls)',
                run_monitor.meta is None,
                detail=repr(run_monitor.meta),
            )

        read = await client.read_resource(apps.RUN_MONITOR_URI)
        text = read.contents[0].text if read.contents else ''
        check('run-monitor bundle length > 10_000 chars', len(text) > 10_000, detail=f'len={len(text)}')
        check("run-monitor bundle contains '<script'", '<script' in text)

        tools = await client.list_tools()
        for tool_name in ('monitor', 'get_pipeline_trace'):
            tool = next((t for t in tools.tools if t.name == tool_name), None)
            check(f'{tool_name} tool present', tool is not None)
            if tool is not None:
                check(
                    f'{tool_name} carries _meta.ui.resourceUri == RUN_MONITOR_URI',
                    tool.meta == {'ui': {'resourceUri': apps.RUN_MONITOR_URI}},
                    detail=repr(tool.meta),
                )

    print()
    if failures:
        print(f'{len(failures)} check(s) FAILED: {failures}')
        return 1
    print('All checks PASSED.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
```

Result:

```
[PASS] list_resources includes ui:// run-monitor resource
[PASS] run-monitor resource carries no _meta.ui.csp (no direct network calls)
[PASS] run-monitor bundle length > 10_000 chars
[PASS] run-monitor bundle contains '<script'
[PASS] monitor tool present
[PASS] monitor carries _meta.ui.resourceUri == RUN_MONITOR_URI
[PASS] get_pipeline_trace tool present
[PASS] get_pipeline_trace carries _meta.ui.resourceUri == RUN_MONITOR_URI

All checks PASSED.
```

Verdict: **PASS** for everything the automated harness can reach — the
run-monitor resource lists with no CSP meta (correct — it makes no direct
network calls), the widget resource reads back as the real built HTML
bundle, and both `monitor` and `get_pipeline_trace` correctly carry
`_meta.ui.resourceUri == RUN_MONITOR_URI`. Steps 1–5 above (real engine
boot, real Streamable HTTP transport, real MCPJam/Claude browser rendering
of the status chip/counts/feed, the poll-stops-at-terminal behavior, and
the widget-initiated-tool-call consent UX) remain **pending manual
execution by Dylan**.

## Slice 4 — pipeline graph (WIDGET REPLACED 2026-08-05 — section retained for history)

> Dylan's decision after the slice-4 automated pre-checks: the SVG
> pipeline-graph widget is replaced by a thin iframe shell embedding the real
> standalone `/canvas` page (slice 5), rather than carried forward as a
> second, divergent graph renderer. Widget source (`main.ts`, `edges.ts`,
> `index.html`), the `edges.test.md` encoding evidence, and the
> `validate_pipeline` widget link are removed; `pipeline-graph` is superseded
> by `pipeline-canvas` (linked to `describe_pipeline` only — see "Slice 5 —
> pipeline canvas" below). Steps below are obsolete; retained only because
> the `.pipe` edge-encoding evidence they reference is still useful context
> (also preserved in git history at commit `5acff373`).

Note: the slice-4 plan's original step referenced
`examples/tool-pipe-diamond.pipe`, which does not exist on this branch. Use
`examples/document-processor.pipe` instead — verified fan-out/fan-in shape:
`parse_1` fans out to both `ocr_1` and `ner_1`; `ner_1` fans back in from
both `parse_1` and `ocr_1`.

1. Build + boot as in steps 1–3 above (widget bundle now includes
   `pipeline-graph` as well as `pipelines-table`/`dropper`/`run-monitor`;
   all four are built by `./builder mcp-widgets:build`).
2. In MCPJam, call `describe_pipeline` with
   `filepath: 'examples/document-processor.pipe'`. The pipeline-graph
   widget must render: a layered left-to-right DAG (pure SVG, no zoom/pan)
   with a node per component, edges only for **data** (`inputs`) wiring,
   and `ner_1` shown with two incoming edges — one from `parse_1`, one from
   `ocr_1` (the fan-in). Hovering an edge should show its lane name (SVG
   `<title>` on each `<path>`, e.g. `text`) as a native tooltip.
3. Make a deliberately broken copy of `examples/document-processor.pipe`
   (e.g. rename one component's `provider` to something invalid, or point
   an `input.from` at a nonexistent component id) and call
   `validate_pipeline` with `filepath` pointing at the broken copy, **in
   the same widget instance** right after step 2. The widget must render
   the returned error list underneath the still-visible graph from step 2,
   and highlight (via a CSS `error` class) any node whose id is
   substring-matched inside an error message.
   **Caveat:** the on-screen graph may originate from a different pipeline than the one being validated — highlights are only meaningful when `describe_pipeline` and `validate_pipeline` ran against the same pipeline.
4. **v1 behavior to confirm, not a bug if observed:** the widget's
   `components` state is only ever populated by a `describe_pipeline`
   result (`validate_pipeline`'s result carries no `components` key at
   all — see `packages/ai/src/ai/modules/mcp/tools/introspection.py`,
   `_validate_pipeline`). Concretely:
   - Node highlighting on `validate_pipeline` errors only happens if a
     `describe_pipeline` call already populated the graph earlier in the
     *same* widget instance (as in steps 2→3 above).
   - Calling `validate_pipeline` **cold** — no prior `describe_pipeline`
     in that widget instance — still renders the returned error/warning
     list; above it, the widget shows the "No components to draw yet —
     run describe_pipeline." hint in place of the graph (no nodes exist to
     draw or highlight yet). To see node highlighting on top of the error
     list, `describe_pipeline` must run first in that widget instance.
     Open a **fresh** widget instance and call `validate_pipeline` alone
     (skip step 2) to confirm the error list renders with the placeholder
     hint standing in for the graph.
5. **Documented blind spot:** confirm the DAG never shows `control`
   connections (an agent wired to its `llm`/`tool`/`memory` components).
   If `examples/document-processor.pipe` or another pipe on hand has an
   agent with a control-only tool/llm, that component should appear as an
   unconnected root node in the graph, not as a child of the agent — this
   is expected v1 behavior (`parseEdges` only reads each component's
   `inputs` array, which never carries control wiring), not a bug to
   chase.

Record results (pass/fail + screenshots) below, following the same format
as the slice-1 "Manual execution" section above.

### Automated pre-checks (2026-08-05)

Extends the slice-1/2/3 automated pre-check pattern with the
pipeline-graph-specific assertions: the resource listing (with no CSP
meta, since pipeline-graph makes no direct network calls of its own — all
data arrives via `ontoolresult`, not `callServerTool`), the widget bundle
read-back, and the `_meta.ui.resourceUri` link on **both** tools the
widget is registered against (`describe_pipeline` and `validate_pipeline`).
Same caveats as the earlier slices — this is the in-process
`mcp.client.Client` harness, not a real engine boot or real browser
rendering; it does not replace steps 1–5 above.

**Environment:** same fast venv python as the slice-1/2/3 pre-checks
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcpv2/bin/python`),
run from `packages/ai` with `PYTHONPATH=src`. Server built via
`ai.modules.mcp.handlers.build_mcp_server` with the **default** `apps_dir`
(reads the real
`packages/ai/src/ai/modules/mcp/apps/dist/pipeline-graph.html`, 340,550
bytes on disk at run time) and a stub `EngineClient` (`list_tasks`/
`deploy_list` return `[]` — enough for the server to build;
`describe_pipeline`/`validate_pipeline` are only inspected via
`list_tools`/`list_resources` here, not called).

Script
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcp_apps_pipeline_graph_smoke.py`):

```python
"""In-process smoke test for the slice-4 pipeline-graph widget (MCP Apps).

Builds the real server (default apps_dir, i.e. apps/dist next to the mcp
module -- NOT a tmp_path override) with a stub engine factory, and drives it
over the in-memory mcp.client.Client the way tests/ai/modules/mcp/test_apps.py
does. Verifies:

  1. list_resources includes the ui:// pipeline-graph resource, with NO
     `_meta.ui.csp` (pipeline-graph makes no direct network calls -- all
     data arrives via ontoolresult, unlike dropper)
  2. read_resource returns the REAL built pipeline-graph bundle (len > 10_000
     chars, contains '<script')
  3. list_tools shows `_meta.ui.resourceUri` == PIPELINE_GRAPH_URI on BOTH
     `describe_pipeline` and `validate_pipeline`

Run with the fast venv python from packages/ai (so `ai` imports resolve):
  cd packages/ai && PYTHONPATH=src <venv>/bin/python /path/to/mcp_apps_pipeline_graph_smoke.py
"""

import asyncio
import sys

from mcp.client import Client

from ai.modules.mcp import apps, handlers


class StubEngineClient:
    """Minimal EngineClient stub -- enough for the server to build and
    list_tools/list_resources/read_resource to run without a live engine."""

    async def list_tasks(self):
        return []

    async def deploy_list(self):
        return []


async def main() -> int:
    failures = []

    def check(label, condition, detail=''):
        status = 'PASS' if condition else 'FAIL'
        print(f'[{status}] {label}' + (f' -- {detail}' if detail and not condition else ''))
        if not condition:
            failures.append(label)

    server = handlers.build_mcp_server(lambda: StubEngineClient())

    async with Client(server) as client:
        listed = await client.list_resources()
        graph = next((r for r in listed.resources if str(r.uri) == apps.PIPELINE_GRAPH_URI), None)
        check(
            'list_resources includes ui:// pipeline-graph resource',
            graph is not None,
            detail=repr([str(r.uri) for r in listed.resources]),
        )
        if graph is not None:
            check(
                'pipeline-graph resource carries no _meta.ui.csp (no direct network calls)',
                graph.meta is None,
                detail=repr(graph.meta),
            )

        read = await client.read_resource(apps.PIPELINE_GRAPH_URI)
        text = read.contents[0].text if read.contents else ''
        check('pipeline-graph bundle length > 10_000 chars', len(text) > 10_000, detail=f'len={len(text)}')
        check("pipeline-graph bundle contains '<script'", '<script' in text)

        tools = await client.list_tools()
        for tool_name in ('describe_pipeline', 'validate_pipeline'):
            tool = next((t for t in tools.tools if t.name == tool_name), None)
            check(f'{tool_name} tool present', tool is not None)
            if tool is not None:
                check(
                    f'{tool_name} carries _meta.ui.resourceUri == PIPELINE_GRAPH_URI',
                    tool.meta == {'ui': {'resourceUri': apps.PIPELINE_GRAPH_URI}},
                    detail=repr(tool.meta),
                )

    print()
    if failures:
        print(f'{len(failures)} check(s) FAILED: {failures}')
        return 1
    print('All checks PASSED.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
```

Result:

```
[PASS] list_resources includes ui:// pipeline-graph resource
[PASS] pipeline-graph resource carries no _meta.ui.csp (no direct network calls)
[PASS] pipeline-graph bundle length > 10_000 chars
[PASS] pipeline-graph bundle contains '<script'
[PASS] describe_pipeline tool present
[PASS] describe_pipeline carries _meta.ui.resourceUri == PIPELINE_GRAPH_URI
[PASS] validate_pipeline tool present
[PASS] validate_pipeline carries _meta.ui.resourceUri == PIPELINE_GRAPH_URI

All checks PASSED.
```

Verdict: **PASS** for everything the automated harness can reach — the
pipeline-graph resource lists with no CSP meta (correct — it makes no
direct network calls), the widget resource reads back as the real built
HTML bundle, and both `describe_pipeline` and `validate_pipeline`
correctly carry `_meta.ui.resourceUri == PIPELINE_GRAPH_URI`. Steps 1–5
above (real engine boot, real Streamable HTTP transport, real
MCPJam/Claude browser rendering of the DAG/fan-in/edge-hover, the
error-list-and-highlight behavior, the cold-`validate_pipeline`
error-list-with-placeholder behavior, and the control-edge blind spot)
remain **pending manual execution by Dylan**.

## Slice 5 — pipeline canvas (WIDGET + CANVAS SURFACE REMOVED 2026-08-05 — section retained for history)

> Dylan ran the manual pass: the embedded canvas render was "not even remotely
> close" to the real canvas view, and per the standing decision (this widget
> only survives as the real canvas), the entire slice-5 surface was removed —
> pipeline-canvas widget, /canvas web module, canvas_stash + canvas_url,
> apps/canvas-ui. describe_pipeline/validate_pipeline are plain JSON tools
> again. Likely root cause (unconfirmed, for any future revival): inline
> pipelines carry no node positions/viewport, and the standalone page mounted
> Canvas without the auto-layout pass the VS Code host runs, so nodes render
> unpositioned. Evidence and full implementation preserved at commit 5731b656.
> Steps below are obsolete.

Slice 4's SVG graph widget is replaced by a thin iframe shell
(`apps/mcp-widgets/src/pipeline-canvas/main.ts`) that embeds the real,
standalone `/canvas` page instead of re-implementing DAG rendering
client-side. `describe_pipeline` mints a capability URL
(`canvas_url`, e.g. `http://<engine>/canvas?pipe=<key>`) via
`ai.modules.mcp.canvas_stash`; the widget parses that key off the tool
result and points a full-width `<iframe>` at it. `validate_pipeline` no
longer links any widget (see doc.md, "Embedded UI (MCP Apps)").

1. Build + boot as in steps 1–3 above (widget bundle now includes
   `pipeline-canvas` in place of `pipeline-graph`; the static `/canvas` page
   itself ships separately, built by `./builder canvas-ui:build` into
   `dist/server/static/canvas/`).
2. In MCPJam, call `describe_pipeline` with
   `filepath: 'examples/document-processor.pipe'`. The pipeline-canvas
   widget must render an `<iframe>` whose `src` is the returned `canvas_url`,
   and the iframe must show the **real** canvas page — the same fan-out
   (`parse_1` → `ocr_1`/`ner_1`) / fan-in (`ner_1` ← both) shape the slice-4
   pass exercised, since it's the same underlying page, not a re-implementation.
3. **Independent fallback check — do this regardless of step 2's outcome.**
   Copy the `canvas_url` string out of the raw tool result (or the widget, if
   it rendered) and open it directly in a plain browser tab, outside MCPJam
   entirely. This isolates the canvas page itself (does it render at all?)
   from the frameDomains enforcement question (does *this host* allow the
   iframe to load it?) — see the next step.
4. **This is the actual go/no-go test: does the host honor `frameDomains`?**
   The server always stamps `csp.frameDomains` with the live engine origin
   on the `pipeline-canvas` resource (see doc.md's "Host-support policy for
   `frameDomains`" paragraph) — whether the iframe actually loads is entirely
   up to whether the connected host reads and enforces that field. Record,
   per host tested (MCPJam, then any Claude surface available):
   - **Pass** — iframe loads, canvas page renders inside MCPJam/Claude.
   - **Widget renders, iframe blank/blocked** — host supports the UI
     extension but does not honor `frameDomains`; note any console/CSP error
     if the host surfaces one.
   - **No widget at all** — host does not support `io.modelcontextprotocol/ui`;
     only the plain JSON result (including the `canvas_url` string) is shown
     — the step-3 direct-tab open is then the only way to see the canvas.
5. **First-load latency.** The `/canvas` page embeds the full shared-ui
   component (MUI + xyflow + elkjs) in its own nested bundle — several MB,
   uncompressed on a local dev engine. Note the observed first-load time in
   the iframe (cold cache) here; this is the number that matters for a future
   cloud deployment, where the engine origin won't be `localhost`.
6. **Expired-key behavior.** `canvas_url`'s `pipe` key is a capability with a
   1-hour TTL (`canvas_stash._TTL_S`). Confirm: opening a `canvas_url` after
   its key has expired (or been evicted past `_MAX_ENTRIES`) shows the
   canvas page's own friendly "unknown or expired key" state, not a raw
   error page or a blank screen — and confirm the fix is simply re-running
   `describe_pipeline` to mint a fresh link (there is no refresh/retry
   affordance on the canvas page itself for an expired key).

Record results (pass/fail + screenshots + the per-host frameDomains matrix
from step 4) below, following the same format as the slice-1 "Manual
execution" section above.

### Automated pre-checks (2026-08-05)

Extends the slice-1/2/3/4 automated pre-check pattern with the
pipeline-canvas-specific assertions: the resource listing carries
`csp.frameDomains` (not `connectDomains` — pipeline-canvas embeds another
origin in an iframe, it makes no direct network call of its own), the widget
bundle read-back, the `_meta.ui.resourceUri` link on `describe_pipeline`
only (`validate_pipeline` carries no ui meta at all now), a real
`describe_pipeline` call producing a `canvas_url`, and a direct
`canvas_stash.get()` round-trip of the key embedded in that URL — the same
lookup the `/canvas` web route (`ai.modules.canvas.canvas.canvas_pipeline`)
performs. Same caveats as the earlier slices — this is the in-process
`mcp.client.Client` harness plus a direct stash-module call, not a real
engine boot, real Streamable HTTP transport, or real browser/iframe
rendering; it does not replace steps 1–6 above.

**Environment:** same fast venv python as the slice-1/2/3/4 pre-checks
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcpv2/bin/python`),
run from `packages/ai` with `PYTHONPATH=src`. Server built via
`ai.modules.mcp.handlers.build_mcp_server` with the **default** `apps_dir`
(reads the real
`packages/ai/src/ai/modules/mcp/apps/dist/pipeline-canvas.html` on disk at
run time) and a stub `EngineClient` exposing `base_url ==
'http://localhost:5565'` (feeds both the CSP-stamping path in
`handlers._on_list_resources` and `_describe_pipeline`'s `canvas_url`
minting) plus no-op `list_tasks`/`deploy_list`. `describe_pipeline` is
called with a small inline pipeline dict (no `filepath` needed — the
assertions don't depend on real component metadata, `get_service` failures
on the stub are swallowed the same way a real lookup failure is).

Script
(`/private/tmp/claude-501/-Users-dylansavage-Desktop-rocketride-server/c28f982b-e20d-4503-b9f7-92e85d8440cf/scratchpad/mcp_apps_pipeline_canvas_smoke.py`):

```python
"""In-process smoke test for the slice-5 pipeline-canvas widget (MCP Apps).

Builds the real server (default apps_dir, i.e. apps/dist next to the mcp
module -- NOT a tmp_path override) with a stub engine factory that also
exposes `base_url` (so both the CSP-stamping path in
`handlers._on_list_resources` and describe_pipeline's canvas_url minting have
an engine origin to work with), and drives it over the in-memory
mcp.client.Client the way tests/ai/modules/mcp/test_apps.py does. Verifies:

  1. list_resources includes the ui:// pipeline-canvas resource, with
     `_meta.ui.csp.frameDomains == [engine origin]` (NOT connectDomains --
     pipeline-canvas embeds another origin in an <iframe>, it does not call
     the engine directly)
  2. read_resource returns the REAL built pipeline-canvas bundle (len > 1_000
     chars, contains '<script' -- NOT '<iframe': the bundle is the widget
     shell script that builds the iframe at runtime, the literal tag is not
     present in the static HTML)
  3. list_tools shows `_meta.ui.resourceUri` == PIPELINE_CANVAS_URI on
     describe_pipeline only (validate_pipeline carries no ui meta at all --
     it is no longer widget-linked, per c2eb3dc0)
  4. calling describe_pipeline (inline pipeline, no filepath needed) returns
     a canvas_url starting with 'http://localhost:5565/canvas?pipe='
  5. GET-level: the stash key minted for that canvas_url round-trips through
     ai.modules.mcp.canvas_stash.get() directly (the same lookup the /canvas
     web route performs) and returns the original pipeline dict

Run with the fast venv python from packages/ai (so `ai` imports resolve):
  cd packages/ai && PYTHONPATH=src <venv>/bin/python /path/to/mcp_apps_pipeline_canvas_smoke.py
"""

import asyncio
import sys
from urllib.parse import urlparse, parse_qs

from mcp.client import Client

from ai.modules.mcp import apps, handlers

ENGINE_ORIGIN = 'http://localhost:5565'

INLINE_PIPELINE = {
    'source': {'name': 'smoke-test-pipe'},
    'components': [
        {'id': 'parse_1', 'provider': 'text_parser', 'input': []},
    ],
}


class StubEngineClient:
    """Minimal EngineClient stub -- enough for the server to build and for
    describe_pipeline to run without a live engine. get_service is
    deliberately NOT implemented: _describe_pipeline calls it inside a
    try/except Exception and treats a failure as "service unknown", so the
    AttributeError from a missing attribute is swallowed the same way a real
    lookup failure would be."""

    @property
    def base_url(self):
        return ENGINE_ORIGIN

    async def list_tasks(self):
        return []

    async def deploy_list(self):
        return []


async def main() -> int:
    failures = []

    def check(label, condition, detail=''):
        status = 'PASS' if condition else 'FAIL'
        print(f'[{status}] {label}' + (f' -- {detail}' if detail and not condition else ''))
        if not condition:
            failures.append(label)

    server = handlers.build_mcp_server(lambda: StubEngineClient())

    async with Client(server) as client:
        listed = await client.list_resources()
        canvas = next((r for r in listed.resources if str(r.uri) == apps.PIPELINE_CANVAS_URI), None)
        check(
            'list_resources includes ui:// pipeline-canvas resource',
            canvas is not None,
            detail=repr([str(r.uri) for r in listed.resources]),
        )
        if canvas is not None:
            check(
                'pipeline-canvas resource carries _meta.ui.csp.frameDomains == [engine origin]',
                canvas.meta == {'ui': {'csp': {'frameDomains': [ENGINE_ORIGIN]}}},
                detail=repr(canvas.meta),
            )

        read = await client.read_resource(apps.PIPELINE_CANVAS_URI)
        text = read.contents[0].text if read.contents else ''
        check('pipeline-canvas bundle length > 1_000 chars', len(text) > 1_000, detail=f'len={len(text)}')
        check("pipeline-canvas bundle contains '<script'", '<script' in text)

        tools = await client.list_tools()
        describe_tool = next((t for t in tools.tools if t.name == 'describe_pipeline'), None)
        check('describe_pipeline tool present', describe_tool is not None)
        if describe_tool is not None:
            check(
                'describe_pipeline carries _meta.ui.resourceUri == PIPELINE_CANVAS_URI',
                describe_tool.meta == {'ui': {'resourceUri': apps.PIPELINE_CANVAS_URI}},
                detail=repr(describe_tool.meta),
            )

        validate_tool = next((t for t in tools.tools if t.name == 'validate_pipeline'), None)
        check('validate_pipeline tool present', validate_tool is not None)
        if validate_tool is not None:
            check(
                'validate_pipeline carries NO ui meta (no longer widget-linked)',
                validate_tool.meta is None,
                detail=repr(validate_tool.meta),
            )

        result = await client.call_tool('describe_pipeline', {'pipeline': INLINE_PIPELINE})
        structured = result.structured_content or {}
        canvas_url = structured.get('canvas_url', '')
        check(
            "describe_pipeline result carries canvas_url starting 'http://localhost:5565/canvas?pipe='",
            canvas_url.startswith(f'{ENGINE_ORIGIN}/canvas?pipe='),
            detail=repr(canvas_url),
        )

        stash_key = parse_qs(urlparse(canvas_url).query).get('pipe', [''])[0] if canvas_url else ''
        check('canvas_url carries a non-empty pipe stash key', bool(stash_key), detail=repr(canvas_url))

        if stash_key:
            from ai.modules.mcp import canvas_stash

            redeemed = canvas_stash.get(stash_key)
            check(
                'canvas_stash.get(stash key) redeems the original pipeline dict',
                redeemed == INLINE_PIPELINE,
                detail=repr(redeemed),
            )

    print()
    if failures:
        print(f'{len(failures)} check(s) FAILED: {failures}')
        return 1
    print('All checks PASSED.')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
```

Result:

```
[PASS] list_resources includes ui:// pipeline-canvas resource
[PASS] pipeline-canvas resource carries _meta.ui.csp.frameDomains == [engine origin]
[PASS] pipeline-canvas bundle length > 1_000 chars
[PASS] pipeline-canvas bundle contains '<script'
[PASS] describe_pipeline tool present
[PASS] describe_pipeline carries _meta.ui.resourceUri == PIPELINE_CANVAS_URI
[PASS] validate_pipeline tool present
[PASS] validate_pipeline carries NO ui meta (no longer widget-linked)
[PASS] describe_pipeline result carries canvas_url starting 'http://localhost:5565/canvas?pipe='
[PASS] canvas_url carries a non-empty pipe stash key
[PASS] canvas_stash.get(stash key) redeems the original pipeline dict

All checks PASSED.
```

Verdict: **PASS** for everything the automated harness can reach — the
pipeline-canvas resource lists with `frameDomains` (not `connectDomains`)
pointed at the live engine origin, the widget resource reads back as the
real built HTML bundle (containing the shell `<script>`, not a static
`<iframe>`), `describe_pipeline` alone carries `_meta.ui.resourceUri`
(`validate_pipeline` carries none), and a real `describe_pipeline` call
mints a `canvas_url` whose stash key redeems the exact original pipeline via
`canvas_stash.get()` — the same path the `/canvas` web route uses. Steps
1–6 above (real engine boot, real Streamable HTTP transport, real
MCPJam/Claude iframe rendering, the frameDomains host-support matrix, the
first-load latency measurement, and the expired-key friendly-error UX)
remain **pending manual execution by Dylan**.
