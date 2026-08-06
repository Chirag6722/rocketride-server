# MCP Apps Widgets — Slice 5: Real Canvas Embed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled SVG pipeline-graph widget with the REAL shared-ui canvas: a standalone engine-served `/canvas` page rendering `<Canvas isReadonly>` for a given pipeline, embedded in-chat by a thin iframe-shell widget via `csp.frameDomains`.

**Architecture:** `describe_pipeline` stashes the loaded pipeline JSON server-side under a random capability key (TTL-bounded) and returns `canvas_url = {base}/canvas?pipe=<key>`. A new tiny web module serves the `canvas-ui` static app (dropper-ui build pattern) plus `GET /canvas/pipeline?pipe=<key>` returning the stashed JSON. The page fetches that + the existing public `GET /services` catalog and mounts the shared-ui `Canvas` read-only — full editor-grade rendering (React Flow, elk auto-layout, real node icons/config) with zero canvas re-implementation. The widget becomes a ~40-line shell that iframes `canvas_url`, declaring `csp.frameDomains: [engine origin]`. **Host-support policy (Dylan, 2026-08-05): hosts that don't honor `frameDomains` simply don't get this widget — graceful absence, no fallback build.**

**Tech Stack:** Python (engine web module + MCP tool change), React 18 + `@rocketride/shared-ui` Canvas via rsbuild (dropper-ui template), vanilla-TS widget shell.

## Global Constraints

Same as `plan-slice2-dropper.md` Global Constraints (branch = tip of `feat/http-mcp`, MIT headers, Python single quotes/ruff, TS single quotes, conventional commits with the Claude trailer, fast test runner + known env-wall, nvm before builder, never commit `pipelines/`). Additions:
- `validate_pipeline` loses its widget link entirely (errors render fine as JSON) — only `describe_pipeline` gets the canvas.
- The old `pipeline-graph` widget (source, `edges.ts`, registration, tests) is REMOVED in Task 4 — git history keeps the encoding evidence; `edges.test.md`'s derivation stays referenced from open-questions.

## Verified contracts (scouted 2026-08-05 — do not re-derive)

- Dropper page bootstrap (`apps/dropper-ui/src/App.tsx:56-99`): reads `?auth=` param, scrubs URL, `setAPIConfig({ROCKETRIDE_URI: window.location.origin})`. The canvas page needs LESS — plain HTTP fetches, no WS client at all.
- `GET /services` (modules/services/services.py:8) is public and returns the same catalog as `rrext_services`; `IServiceCatalog` is the result's `services` field unwrapped (see `shell-ui/src/connection/connection.ts:930-931`).
- `Canvas` required props: `oauth2RootUrl`, `project`, `servicesJson`, `handleValidatePipeline` (optional in context, stub `async () => ({status: 'ok', data: {}})` is sanctioned); `isReadonly: true` locks editing and hides the toolbar (Canvas.tsx:139-149, FlowPreferencesContext.tsx:195). Mutation callbacks omitted = buttons hidden (ProjectView.tsx:502 pattern).
- `IProject extends PipelineConfig` with no added fields — exactly the `.pipe` JSON `describe_pipeline` already loads via `load_pipeline(args)` (introspection.py:63).
- Web route registration + public marking: mirror `modules/dropper/dropper.py:23` + `dropper/__init__.py:42,50` (`add_route(..., public=True)`); auth middleware short-circuits public routes (middleware.py:20, server.py:807).
- Canvas source: `packages/shared-ui/src/components/canvas/` (pure props, zero network code); VS Code consumes it via rsbuild `resolve.alias` `shared` → `packages/shared-ui/src` (apps/vscode/rsbuild.config.mjs) — canvas-ui should use the same alias approach; pull the canvas-relevant dependency subset (react, MUI 6 + emotion, `@xyflow/react` ~12.3.4, `elkjs`, `dagre`, `@rjsf/*` v5 + `validator-ajv8`) from `apps/vscode/package.json` versions.
- Static serving: build output copied to `dist/server/static/canvas` (dropper task: `apps/dropper-ui/scripts/tasks.js:22`); dropper-ui is standalone, NOT a shell-ui MF remote — no apps.json registration.
- `client.base_url` on the engine client mints the origin (used by `run_dropper_pipe`, execution.py:229).

---

### Task 1: Pipeline stash + `canvas_url` on describe_pipeline

**Files:**
- Create: `packages/ai/src/ai/modules/mcp/canvas_stash.py`
- Modify: `packages/ai/src/ai/modules/mcp/tools/introspection.py` (describe_pipeline handler)
- Modify: `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Produces: `canvas_stash.put(pipeline: dict) -> str` (random url-safe key, TTL 3600s, max 200 entries — oldest pruned); `canvas_stash.get(key: str) -> Optional[dict]` (None on unknown/expired); `describe_pipeline` result gains `'canvas_url': f'{client.base_url}/canvas?pipe={key}'` (only when `client.base_url` is truthy; omit the field otherwise).

- [ ] **Step 1: Failing tests** (append to test_apps.py):

```python
def test_canvas_stash_roundtrip_and_expiry(monkeypatch):
    from ai.modules.mcp import canvas_stash

    key = canvas_stash.put({'components': []})
    assert canvas_stash.get(key) == {'components': []}
    assert canvas_stash.get('nope') is None
    monkeypatch.setattr(canvas_stash, '_now', lambda: canvas_stash._now() + 4000)
    assert canvas_stash.get(key) is None


@pytest.mark.asyncio
async def test_describe_pipeline_returns_canvas_url(fake_engine):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.call_tool(
            'describe_pipeline', {'pipeline': {'components': []}}
        )
    payload = json.loads(result.content[0].text)
    assert payload['ok'] is True
    assert payload['canvas_url'].endswith('?pipe=' + payload['canvas_url'].split('=')[-1])
    assert '/canvas?pipe=' in payload['canvas_url']
```

(The `fake_engine` fixture may lack `base_url` — check conftest; if absent, monkeypatch a `base_url` property onto it in the test, and add a companion test asserting the field is OMITTED when `base_url` raises/missing.)

- [ ] **Step 2: verify failure. Step 3: implement.**

`canvas_stash.py`:

```python
# Copyright 2026 Aparavi Software AG. MIT License.
"""Short-lived server-side stash of pipeline JSONs for the /canvas page.

describe_pipeline loads a pipeline (inline or filepath) without starting a
task, so there is no pk_ token to hand the canvas page. Instead the loaded
JSON is stashed under a random capability key with a bounded TTL; the web
module's GET /canvas/pipeline endpoint redeems it. Keys are 144-bit random —
possession of the URL is the capability, same trust model as pk_ drop links.
"""

import secrets
import time
from typing import Dict, Optional, Tuple

_TTL_S = 3600
_MAX_ENTRIES = 200
_stash: Dict[str, Tuple[float, dict]] = {}


def _now() -> float:
    return time.monotonic()


def _prune() -> None:
    now = _now()
    expired = [k for k, (exp, _) in _stash.items() if exp <= now]
    for k in expired:
        del _stash[k]
    while len(_stash) > _MAX_ENTRIES:
        del _stash[next(iter(_stash))]


def put(pipeline: dict) -> str:
    _prune()
    key = secrets.token_urlsafe(18)
    _stash[key] = (_now() + _TTL_S, pipeline)
    return key


def get(key: str) -> Optional[dict]:
    _prune()
    entry = _stash.get(key)
    return entry[1] if entry else None
```

`introspection.py` describe handler: after the pipeline loads successfully and before building the result dict, add:

```python
    canvas_url = None
    try:
        base = client.base_url
        if base:
            from .. import canvas_stash

            canvas_url = f'{base}/canvas?pipe={canvas_stash.put(pipeline)}'
    except Exception:  # noqa: BLE001 - canvas link is an enhancement, never a failure
        canvas_url = None
```

and include `**({'canvas_url': canvas_url} if canvas_url else {})` in the returned dict (adjust to the handler's actual result-building style — read it first; the substance: field present only when mintable).

- [ ] **Step 4: module suite green (fast runner). Step 5: commit** — `feat(ai,mcp): canvas_url on describe_pipeline via pipeline stash`

---

### Task 2: `/canvas` web module (static page + pipeline endpoint)

**Files:**
- Create: `packages/ai/src/ai/modules/canvas/__init__.py`, `packages/ai/src/ai/modules/canvas/canvas.py`
- Modify: whatever registers modules (mirror how `modules/dropper` is enabled — check the module allowlist the env-walled registration test asserts, `packages/ai/src/ai/web/` or module discovery; find `dropper`'s entry and add `canvas` beside it)
- Modify: `packages/ai/tests/ai/modules/mcp/test_apps.py` (stash-endpoint unit test via the handler function directly)

**Interfaces:**
- Produces: `GET /canvas` + `GET /canvas/{file_path:path}` serving `dist/server/static/canvas` (public, mirror dropper.py byte-for-byte with names swapped); `GET /canvas/pipeline?pipe=<key>` returning the stashed JSON as `application/json` or 404 `{'error': 'unknown or expired key'}` (public — the key IS the auth, per the stash docstring). NOTE route ordering: register `/canvas/pipeline` BEFORE the `/canvas/{file_path:path}` catch-all so it isn't shadowed (verify how dropper's router orders; adjust if the framework matches most-specific-first automatically).

- [ ] **Step 1:** Read `modules/dropper/{__init__.py,dropper.py}` fully; transcribe the envelope for canvas with the extra JSON endpoint whose handler is:

```python
async def canvas_pipeline(request):
    from ai.modules.mcp import canvas_stash

    key = request.query_params.get('pipe', '')
    pipeline = canvas_stash.get(key)
    if pipeline is None:
        return JSONResponse({'error': 'unknown or expired key'}, status_code=404)
    return JSONResponse(pipeline)
```

(match the module's actual response helper imports — dropper.py shows the idiom).

- [ ] **Step 2: unit test** the handler logic (stash a pipeline, call the handler with a stub request object exposing `query_params`, assert 200 payload and 404 path). Append to test_apps.py.
- [ ] **Step 3: fast suite green; `./builder ai:build` runs clean (module registered — confirm no allowlist assertion breaks; the env-walled registration test runs under Task 5's full builder gate).**
- [ ] **Step 4: commit** — `feat(ai,web): /canvas static mount + stashed-pipeline endpoint`

---

### Task 3: canvas-ui standalone app

**Files:**
- Create: `apps/canvas-ui/` — `package.json`, `rsbuild.config.mts`, `tsconfig.json`, `src/index.tsx`, `src/App.tsx`, `scripts/tasks.js`
- Modify: `pnpm-workspace.yaml` (add `apps/canvas-ui` under Applications, alphabetical)

**Interfaces:**
- Consumes: `GET /canvas/pipeline?pipe=<key>` (Task 2), public `GET /services`, `Canvas` from shared-ui via the `shared` rsbuild alias.
- Produces: `builder canvas-ui:build` → bundle copied to `dist/server/static/canvas` (dropper-ui task pattern, hash-cached, key `canvas-ui.buildHash`).

- [ ] **Step 1: scaffold from dropper-ui** — copy `apps/dropper-ui`'s `rsbuild.config.mts`/`tsconfig.json`/`scripts/tasks.js` shapes, renamed (`@rocketride/canvas-ui`, output `SERVER_STATIC_DIR = dist/server/static/canvas`). Add the `shared` alias exactly as `apps/vscode/rsbuild.config.mjs` declares it. package.json deps: react/react-dom plus the canvas subset pinned to `apps/vscode/package.json`'s versions (MUI 6 + emotion, `@xyflow/react`, `elkjs`, `dagre`, `@rjsf/core|mui|utils` + `validator-ajv8`); devDeps mirror dropper-ui. Drop dropper-ui's WS-client/Tailwind bits — not needed.
- [ ] **Step 2: App.tsx** (complete):

```tsx
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 *
 * Standalone read-only canvas page. Loaded as {base}/canvas?pipe=<key>;
 * fetches the stashed pipeline JSON + the public services catalog, then
 * mounts the shared-ui Canvas exactly as VS Code does, minus editing.
 */
import { useEffect, useState } from 'react';
import { Canvas } from 'shared';

export default function App() {
	const [project, setProject] = useState<object | null>(null);
	const [services, setServices] = useState<object | null>(null);
	const [error, setError] = useState('');

	useEffect(() => {
		const key = new URLSearchParams(window.location.search).get('pipe') ?? '';
		if (!key) {
			setError('Missing pipe key.');
			return;
		}
		Promise.all([
			fetch(`/canvas/pipeline?pipe=${encodeURIComponent(key)}`).then((r) => {
				if (!r.ok) throw new Error('Pipeline link expired — re-run describe_pipeline.');
				return r.json();
			}),
			fetch('/services').then((r) => r.json()),
		])
			.then(([pipe, svc]) => {
				setProject(pipe);
				setServices(svc.services ?? svc);
			})
			.catch((e: Error) => setError(e.message));
	}, []);

	if (error) return <p style={{ padding: 16, fontFamily: 'system-ui' }}>{error}</p>;
	if (!project || !services) return <p style={{ padding: 16, fontFamily: 'system-ui' }}>Loading…</p>;
	return (
		<div style={{ width: '100vw', height: '100vh' }}>
			<Canvas
				oauth2RootUrl={window.location.origin}
				project={project as never}
				servicesJson={services as never}
				isReadonly
				handleValidatePipeline={async () => ({ status: 'ok', data: {} }) as never}
			/>
		</div>
	);
}
```

(Adjust the `Canvas` import path to what the `shared` alias actually exports — check `packages/shared-ui/src/index.ts`; fix the two `as never` casts to the real types `IProject`/`IServiceCatalog` imported from shared — they're placeholders only if the types aren't exported. Verify `/services` response shape live: unwrap `services` field per connection.ts:930.)

- [ ] **Step 3:** `pnpm install`; `./builder canvas-ui:build`; verify `dist/server/static/canvas/index.html` + assets exist; `./builder --list-modules` shows canvas-ui.
- [ ] **Step 4: manual smoke** (engine running): `describe_pipeline` via any client (or curl-free python one-liner against the fast venv is fine — actually simplest: run the MCP smoke pattern to get a canvas_url) → open the URL in a browser → the real canvas renders the pipeline read-only. Record a screenshot path in the report. If the engine can't run in the execution environment, mark this step pending-manual and say so.
- [ ] **Step 5: commit** — `feat(canvas-ui): standalone read-only canvas page served at /canvas`

---

### Task 4: Widget swap — iframe shell in, SVG graph out

**Files:**
- Create: `apps/mcp-widgets/src/pipeline-canvas/index.html`, `apps/mcp-widgets/src/pipeline-canvas/main.ts`
- Delete: `apps/mcp-widgets/src/pipeline-graph/` (all files)
- Modify: `apps/mcp-widgets/scripts/tasks.cjs` (WIDGETS: remove `pipeline-graph`, add `pipeline-canvas`), `packages/ai/src/ai/modules/mcp/apps.py` (replace `PIPELINE_GRAPH_URI`/spec with `PIPELINE_CANVAS_URI = 'ui://rocketride/pipeline-canvas.html'`, spec `filename='pipeline-canvas.html'`, `title='Pipeline canvas'`, `needs_engine_frame=True`; add the `needs_engine_frame: bool = False` field and extend `list_ui_resources` to stamp `frameDomains` — see below), `packages/ai/src/ai/modules/mcp/tools/introspection.py` (describe_pipeline: `ui_resource_uri=PIPELINE_CANVAS_URI`; validate_pipeline: REMOVE its `ui_resource_uri` kwarg), `packages/ai/tests/ai/modules/mcp/test_apps.py` (replace the graph link test; add a frameDomains stamping test)

**Interfaces:**
- `AppSpec` gains `needs_engine_frame: bool = False`. `list_ui_resources` builds csp from both flags:

```python
        csp = {}
        if spec.needs_engine_origin and engine_origin:
            csp['connectDomains'] = [engine_origin]
        if spec.needs_engine_frame and engine_origin:
            csp['frameDomains'] = [engine_origin]
        meta = {'ui': {'csp': csp}} if csp else None
```

- [ ] **Step 1: failing tests** — replace `test_introspection_tools_link_pipeline_graph_widget` with: describe_pipeline carries `PIPELINE_CANVAS_URI` meta, `validate_pipeline` meta is None, canvas resource listed with `meta == {'ui': {'csp': {'frameDomains': ['http://localhost:5565']}}}` when origin known (monkeypatch pattern from the slice-2 CSP test).
- [ ] **Step 2: widget** — `main.ts` (complete; header comment per convention):

```ts
import { App } from '@modelcontextprotocol/ext-apps';

const app = new App({ name: 'RocketRide pipeline canvas', version: '0.1.0' });
const root = document.getElementById('root') as HTMLElement;

function parseUrl(result: unknown): string | null {
	const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
	const text = content.find((c) => c.type === 'text')?.text;
	if (!text) return null;
	try {
		const payload = JSON.parse(text) as { ok?: boolean; canvas_url?: string };
		return payload.ok && payload.canvas_url ? payload.canvas_url : null;
	} catch {
		return null;
	}
}

app.ontoolresult = (result) => {
	root.classList.remove('empty');
	const url = parseUrl(result);
	if (!url) {
		root.textContent =
			'No canvas link in the result (engine origin unknown, or an older server).';
		return;
	}
	const frame = document.createElement('iframe');
	frame.src = url;
	frame.title = 'Pipeline canvas';
	frame.style.width = '100%';
	frame.style.height = '600px';
	frame.style.border = 'none';
	root.replaceChildren(frame);
};
app.connect();
```

index.html: standard skeleton, `.empty` class, `html, body, #root { height: 100% }`.

- [ ] **Step 3:** typecheck; `./builder mcp-widgets:build` → dist has `pipelines-table.html`, `dropper.html`, `pipeline-canvas.html` and NO `pipeline-graph.html` (also delete the stale copy from `packages/ai/src/ai/modules/mcp/apps/dist/` if the mirror didn't); fast suite green.
- [ ] **Step 4: commit** — `feat(ai,mcp,mcp-widgets)!: replace pipeline-graph widget with real-canvas iframe shell`

---

### Task 5: E2E + docs

- [ ] **Step 1:** `./builder ai:test` full — green (env-walled registration test must still pass under builder with the new canvas module registered).
- [ ] **Step 2:** `./builder ai:build` then confirm `dist/server/static/canvas/` and the three widget bundles in dist; remove any stale `pipeline-graph.html`/`run-monitor.html` leftovers in `dist/server/ai/modules/mcp/apps/dist/` (syncDir doesn't mirror deletions).
- [ ] **Step 3:** doc.md — rewrite the pipeline widget paragraph: pipeline-canvas iframe shell + `frameDomains` + the stash/`canvas_url` flow + host-support policy (hosts that don't honor frameDomains show the JSON result only); note validate_pipeline is no longer widget-linked. Runbook: mark the slice-4 graph section removed (like the run-monitor precedent), append a "Slice 5 — pipeline canvas" section (manual: describe_pipeline in MCPJam → iframe renders the REAL canvas; also open canvas_url directly in a browser tab as the fallback check; Claude pass = the frameDomains go/no-go, record which hosts render). Update open-questions: 12b parsePayload now 2-3 widgets (recount), add "frameDomains host-support matrix" as the live question, mark #11 (edge encoding) closed-and-superseded.
- [ ] **Step 4: commit** — `docs(ai,mcp): pipeline-canvas docs + runbook; graph-widget removal notes`

## Known risks

- **frameDomains host enforcement** is the whole bet, and per Dylan's policy a host that blocks it just doesn't show the widget — MCPJam + direct-URL checks keep us honest about which failure is whose.
- The canvas page loads the full shared-ui component in a nested iframe — bundle will be several MB (MUI + xyflow + elkjs). Fine for a local engine; note first-load latency in the runbook for cloud.
- `pipe` keys are capability URLs with a 1h TTL — a re-run of `describe_pipeline` mints a fresh link; expired links show the page's friendly error.
