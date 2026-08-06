# MCP Apps Widgets — Slice 2: Dropper Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the flagship dropper widget — drag-drop file upload with progress and in-chat results, attached to `run_dropper_pipe` — plus the slice-1 carry-overs (central `structuredContent`, serverInfo version, Terminate-button catch) and the serve-time CSP mechanism it needs.

**Architecture:** The widget uploads via `XMLHttpRequest` directly to the `pk_`-tokenized `upload_url` from the tool result (XHR, not fetch — fetch has no upload-progress events). The POST is **synchronous**: its response body is the complete pipeline result (`DataResult`), so no WS and no polling — upload, then render. Cross-origin fetch from the sandboxed iframe requires the engine origin in the widget's CSP, so `apps.py` learns to stamp `_meta.ui.csp.connectDomains` at list time using the engine client's `base_url`. Renderers are vanilla TS (JSON/text/markdown-as-text plus `<img>/<audio>/<video>` from base64 data URLs) — porting dropper-ui's React Views is deliberately deferred; it would triple the bundle for polish, not capability.

**Tech Stack:** Python (`mcp` 2.0.x low-level Server), TypeScript + Vite + `vite-plugin-singlefile` + `@modelcontextprotocol/ext-apps` ^1.7.0 (no React), pytest in-memory `Client` harness.

## Global Constraints

- Base: tip of `feat/http-mcp` (slice 1 merged there; currently `9e7add83`). Same branch unless Dylan has split it by execution time — check with him if the branch state looks unexpected.
- MIT license header on all new source files (`# Copyright 2026 Aparavi Software AG. MIT License.` for Python; the block-comment form used in `apps/mcp-widgets/src/pipelines-table/main.ts` for TS).
- Python: single quotes, ruff clean, 3.10+. TypeScript: single quotes, strict, ES2022. Conventional commits ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- MCP module suite must stay green. Fast runner (from `packages/ai`): a venv with `mcp>=2,<3`, `pytest`, `pytest-asyncio`, `time_machine`, `fastapi`, `python-dotenv`, `httpx`, `-e packages/client-python`, and a no-op `depends.py` stubbed into site-packages; `python -m pytest tests/ai/modules/mcp/ -q -p no:cacheprovider`. One known env-wall failure (`test_module_registration`) passes only under `./builder ai:test`.
- Builder needs nvm first: `export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22.22.3`. If the dist engine SIGKILLs ("exit null"), `codesign --force -s - dist/server/engine`.
- Widget bundles: single self-contained HTML (no external `src`), built via `./builder mcp-widgets:build`, output synced to `packages/ai/src/ai/modules/mcp/apps/dist/`.
- Never stage/commit anything under `pipelines/`.

## Verified contracts (do not re-derive; scouted 2026-08-03 from the live branch)

- `run_dropper_pipe` returns `{'ok': True, 'task_token', 'upload_url', 'dropper_url'}` (+ `flow_subscribed` when `pipelineTraceLevel` set); `upload_url = {base}/task/data?token=<tk>&auth=<pk>` (execution.py:224-243).
- `POST upload_url` (task_http/task_data.py:432): multipart form — **arbitrary field names**, each field one object (dupes become `key_0, key_1`); non-multipart bodies land under key `'body'`. Blocks until the pipeline finishes. Response: `{'objectsRequested': int, 'objectsCompleted': int, 'resultTypes': {<key>: <type info>}, 'objects': {<key>: <pipe result dict>}}`.
- `CallToolResult` in SDK v2 has `structured_content: Any = None` (verified in installed `mcp_types/_types.py:1463`). Anchor: handlers.py:147.
- `_on_list_resources` closure has `engine_factory` in scope (handlers.py:70-76,149); `WsEngineClient.base_url` is an env-derived property (engine.py:72-84) — no connection needed to read it.
- `apps/mcp-widgets` build: `WIDGETS` array in `apps/mcp-widgets/scripts/tasks.cjs`, per-widget `WIDGET=<name>` Vite invocations, `dist/index.html` renamed to `<widget>.html`, synced to the Python package.

---

### Task 1: Central `structuredContent` + serverInfo version

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/handlers.py` (line ~147 result construction; Server construction at the bottom of `build_mcp_server`)
- Modify: `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Produces: every tool result now carries `structured_content` == the handler's dict (ChatGPT-ready; harmless elsewhere); `initialize` reports a non-empty server version.

- [ ] **Step 1: Write the failing tests** (append to `test_apps.py`):

```python
@pytest.mark.asyncio
async def test_tool_results_carry_structured_content(fake_engine):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        result = await client.call_tool('list_running_pipelines', {})
    assert result.structured_content is not None
    assert result.structured_content['ok'] is True
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.asyncio
async def test_server_reports_nonempty_version(fake_engine):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine)
    async with Client(server) as client:
        info = client.server_info
    assert info is not None and info.version
```

(`import json` may already be present in the file; add if not. If `client.server_info` isn't the v2 accessor, mirror how `test_handlers.py`/`test_dual_revision.py` reach the initialize result — the substance is a non-empty version string.)

- [ ] **Step 2: Run to verify both fail**

Run: `python -m pytest tests/ai/modules/mcp/test_apps.py -q -p no:cacheprovider`
Expected: FAIL — `structured_content` is None; version is `''`.

- [ ] **Step 3: Implement**

In `handlers.py`, change line ~147 to:

```python
        return types.CallToolResult(
            content=[types.TextContent(type='text', text=json.dumps(result, default=str))],
            structured_content=result,
        )
```

For the version, add near the imports:

```python
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    _SERVER_VERSION = _pkg_version('ai')
except PackageNotFoundError:
    _SERVER_VERSION = '0.0.0'
```

(Check `packages/ai/pyproject.toml` `[project] name` first — if the distribution isn't named `ai`, use the real name.) Then pass it in the Server constructor: `server = Server('rocketride-mcp', version=_SERVER_VERSION, ...)`.

- [ ] **Step 4: Full module suite green**

Run: `python -m pytest tests/ai/modules/mcp/ -q -p no:cacheprovider`
Expected: all pass except the known env-wall.

- [ ] **Step 5: Commit** — `feat(ai,mcp): structured_content on tool results; real server version`

---

### Task 2: Terminate-button catch fix (pipelines-table)

**Files:**
- Modify: `apps/mcp-widgets/src/pipelines-table/main.ts`

- [ ] **Step 1: Implement** — wrap the Terminate handler:

```ts
		stop.onclick = async () => {
			stop.disabled = true;
			try {
				await app.callServerTool({ name: 'terminate', arguments: { task_token: row.token } });
				await refresh();
			} catch (err) {
				stop.disabled = false;
				stop.textContent = 'Terminate (failed — retry)';
				console.error('terminate failed', err);
			}
		};
```

Also wrap the Refresh handler and initial `refresh()` calls in try/catch that surface a one-line error into `root` (keep `textContent`, never `innerHTML`, for the message).

- [ ] **Step 2: Typecheck + rebuild + verify single-file**

```bash
cd apps/mcp-widgets && pnpm run typecheck && cd ../..
./builder mcp-widgets:build
grep -c 'src=' packages/ai/src/ai/modules/mcp/apps/dist/pipelines-table.html
```
Expected: typecheck clean; build emits; no external script `src`.

- [ ] **Step 3: Commit** — `fix(mcp-widgets): error handling on terminate/refresh in pipelines-table`

---

### Task 3: Serve-time CSP stamping in apps.py

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/apps.py`, `packages/ai/src/ai/modules/mcp/handlers.py`
- Modify: `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Produces: `AppSpec` gains `needs_engine_origin: bool = False`; `list_ui_resources(apps_dir=None, engine_origin: Optional[str] = None)` stamps `meta={'ui': {'csp': {'connectDomains': [engine_origin]}}}` on specs with `needs_engine_origin=True` when an origin is available; `_on_list_resources` computes the origin from `engine_factory().base_url` defensively.

- [ ] **Step 1: Write the failing tests** (append to `test_apps.py`):

```python
def test_ui_resource_csp_stamped_when_origin_known(apps_dir, monkeypatch):
    from ai.modules.mcp import apps

    spec = apps.AppSpec(
        uri='ui://rocketride/x.html', filename='pipelines-table.html',
        title='X', needs_engine_origin=True,
    )
    monkeypatch.setattr(apps, 'APPS', [spec])
    listed = apps.list_ui_resources(apps_dir, engine_origin='http://localhost:5565')
    assert listed[0].meta == {'ui': {'csp': {'connectDomains': ['http://localhost:5565']}}}


def test_ui_resource_no_csp_without_origin_or_flag(apps_dir):
    from ai.modules.mcp import apps

    listed = apps.list_ui_resources(apps_dir)
    assert all(r.meta is None for r in listed)
```

- [ ] **Step 2: Run to verify failure** — `TypeError: unexpected keyword argument 'needs_engine_origin'`.

- [ ] **Step 3: Implement**

`apps.py`: add `needs_engine_origin: bool = False` to `AppSpec`; change `list_ui_resources`:

```python
def list_ui_resources(
    apps_dir: Optional[Path] = None, engine_origin: Optional[str] = None
) -> List[types.Resource]:
    out = []
    for spec in available_apps(apps_dir):
        meta = None
        if spec.needs_engine_origin and engine_origin:
            meta = {'ui': {'csp': {'connectDomains': [engine_origin]}}}
        out.append(
            types.Resource(uri=spec.uri, name=spec.title, mimeType=UI_MIME_TYPE, meta=meta)
        )
    return out
```

`handlers.py` `_on_list_resources`: compute the origin defensively and pass it:

```python
        try:
            engine_origin = engine_factory().base_url
        except Exception:  # noqa: BLE001 - origin is an enhancement, never a failure
            engine_origin = None
        return types.ListResourcesResult(
            resources=resources_mod.list_resources()
            + apps_mod.list_ui_resources(apps_dir, engine_origin),
            ttl_ms=RESOURCES_LIST_TTL_MS,
            cache_scope=CACHE_SCOPE,
        )
```

(`fake_engine` has no `base_url` attribute — that's exactly what the except covers; existing tests asserting `meta is None` on the pipelines-table resource must still pass since its spec doesn't set the flag.)

- [ ] **Step 4: Module suite green.** Step 5: **Commit** — `feat(ai,mcp): serve-time csp connectDomains stamping for widgets`

---

### Task 4: The dropper widget

**Files:**
- Create: `apps/mcp-widgets/src/dropper/index.html`, `apps/mcp-widgets/src/dropper/main.ts`

**Interfaces:**
- Consumes: `ontoolresult` push of the `run_dropper_pipe` result (`{ok, task_token, upload_url, dropper_url}`); XHR POST multipart to `upload_url`; response `DataResult` per the verified contract.
- Produces: bundle `dropper.html` (wired in Task 5).

- [ ] **Step 1: index.html** — same skeleton/style approach as `pipelines-table/index.html` (color-scheme, system font, `.empty` class), plus styles for a dropzone (`.dropzone`, `.dropzone.drag` highlight, dashed border), a progress bar (`.bar > .fill` width-percentage), and a results area (`.result-object` card, `img,video { max-width: 100% }`). Root div id `root`, script `./main.ts`. Title `Drop files`.

- [ ] **Step 2: main.ts** — complete implementation:

```ts
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 *
 * Dropper widget: drag-drop upload to the pk_-tokenized upload_url from the
 * run_dropper_pipe tool result. The POST is synchronous — its response is the
 * full DataResult {objectsRequested, objectsCompleted, resultTypes, objects}.
 * XHR (not fetch) for upload-progress events. Requires csp.connectDomains to
 * include the engine origin (stamped by apps.py at list time).
 */
import { App } from '@modelcontextprotocol/ext-apps';

interface DropperInfo {
	upload_url: string;
	dropper_url: string;
	task_token: string;
}

interface DataResult {
	objectsRequested: number;
	objectsCompleted: number;
	resultTypes: Record<string, unknown>;
	objects: Record<string, unknown>;
}

const app = new App({ name: 'RocketRide dropper', version: '0.1.0' });
const root = document.getElementById('root') as HTMLElement;
let info: DropperInfo | null = null;

function parseInfo(result: unknown): DropperInfo | null {
	const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
	const text = content.find((c) => c.type === 'text')?.text;
	if (!text) return null;
	try {
		const payload = JSON.parse(text) as Partial<DropperInfo> & { ok?: boolean };
		return payload.ok && payload.upload_url
			? (payload as DropperInfo)
			: null;
	} catch {
		return null;
	}
}

function el<K extends keyof HTMLElementTagNameMap>(
	tag: K, cls?: string, text?: string,
): HTMLElementTagNameMap[K] {
	const node = document.createElement(tag);
	if (cls) node.className = cls;
	if (text !== undefined) node.textContent = text;
	return node;
}

/** Render one pipe result object. Media fields arrive as base64 with a
 * mime type (dropper-ui pattern: data:${mime_type};base64,${data}). */
function renderObject(key: string, value: unknown): HTMLElement {
	const card = el('div', 'result-object');
	card.appendChild(el('h3', undefined, key));
	if (value !== null && typeof value === 'object') {
		const record = value as Record<string, unknown>;
		const mime = typeof record.mime_type === 'string' ? record.mime_type : '';
		const data = typeof record.data === 'string' ? record.data : '';
		if (mime && data) {
			const url = `data:${mime};base64,${data}`;
			if (mime.startsWith('image/')) {
				const img = el('img');
				img.src = url;
				card.appendChild(img);
				return card;
			}
			if (mime.startsWith('audio/') || mime.startsWith('video/')) {
				const media = el(mime.startsWith('audio/') ? 'audio' : 'video');
				media.controls = true;
				media.src = url;
				card.appendChild(media);
				return card;
			}
		}
		if (typeof record.text === 'string') {
			card.appendChild(el('pre', undefined, record.text));
			return card;
		}
	}
	if (typeof value === 'string') {
		card.appendChild(el('pre', undefined, value));
		return card;
	}
	card.appendChild(el('pre', undefined, JSON.stringify(value, null, 2)));
	return card;
}

function renderResults(data: DataResult): void {
	const wrap = el('div');
	wrap.appendChild(
		el('p', undefined, `${data.objectsCompleted}/${data.objectsRequested} objects processed`),
	);
	for (const [key, value] of Object.entries(data.objects ?? {})) {
		wrap.appendChild(renderObject(key, value));
	}
	root.replaceChildren(wrap, buildDropzone('Drop more files'));
}

function upload(files: FileList | File[]): void {
	if (!info) return;
	const form = new FormData();
	Array.from(files).forEach((f, i) => form.append(`file_${i}`, f, f.name));
	const bar = el('div', 'bar');
	const fill = el('div', 'fill');
	bar.appendChild(fill);
	const label = el('p', undefined, 'Uploading…');
	root.replaceChildren(label, bar);

	const xhr = new XMLHttpRequest();
	xhr.open('POST', info.upload_url);
	xhr.upload.onprogress = (e) => {
		if (e.lengthComputable) fill.style.width = `${Math.round((e.loaded / e.total) * 100)}%`;
	};
	xhr.upload.onload = () => {
		label.textContent = 'Processing… (the pipeline is running; this can take a while)';
	};
	xhr.onload = () => {
		try {
			renderResults(JSON.parse(xhr.responseText) as DataResult);
		} catch {
			root.replaceChildren(
				el('p', 'empty', `Unexpected response (HTTP ${xhr.status})`),
				buildDropzone('Try again'),
			);
		}
	};
	xhr.onerror = () => {
		root.replaceChildren(
			el('p', 'empty', 'Upload failed — network/CSP error. Check the engine is reachable.'),
			buildDropzone('Try again'),
		);
	};
	xhr.send(form);
}

function buildDropzone(prompt: string): HTMLElement {
	const zone = el('div', 'dropzone', prompt);
	const picker = el('input') as HTMLInputElement;
	picker.type = 'file';
	picker.multiple = true;
	picker.style.display = 'none';
	picker.onchange = () => picker.files && upload(picker.files);
	zone.appendChild(picker);
	zone.onclick = () => picker.click();
	zone.ondragover = (e) => { e.preventDefault(); zone.classList.add('drag'); };
	zone.ondragleave = () => zone.classList.remove('drag');
	zone.ondrop = (e) => {
		e.preventDefault();
		zone.classList.remove('drag');
		if (e.dataTransfer?.files.length) upload(e.dataTransfer.files);
	};
	return zone;
}

app.ontoolresult = (result) => {
	info = parseInfo(result);
	root.classList.remove('empty');
	if (!info) {
		root.textContent = 'run_dropper_pipe did not return an upload URL.';
		return;
	}
	root.replaceChildren(buildDropzone('Drop files here, or click to choose'));
};
app.connect();
```

- [ ] **Step 3: Typecheck** — `cd apps/mcp-widgets && pnpm run typecheck`. Commit with Task 5 (the widget isn't buildable standalone until registered there).

---

### Task 5: Register + link the dropper widget

**Files:**
- Modify: `apps/mcp-widgets/scripts/tasks.cjs` (`WIDGETS` array), `packages/ai/src/ai/modules/mcp/apps.py` (`APPS`), `packages/ai/src/ai/modules/mcp/tools/execution.py` (`run_dropper_pipe` registration), `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Produces: `DROPPER_URI = 'ui://rocketride/dropper.html'` in apps.py; spec `AppSpec(uri=DROPPER_URI, filename='dropper.html', title='Drop files', needs_engine_origin=True)`; `run_dropper_pipe` registered with `ui_resource_uri=DROPPER_URI`; `WIDGETS = ['pipelines-table', 'dropper']`.

- [ ] **Step 1: Failing tests** (append to `test_apps.py`; the `apps_dir` fixture needs a second file — extend it or write `dropper.html` inside the test):

```python
@pytest.mark.asyncio
async def test_run_dropper_pipe_links_dropper_widget(fake_engine, tmp_path):
    import ai.modules.mcp.handlers as handlers_mod

    (tmp_path / 'dropper.html').write_text('<!doctype html><html><body>d</body></html>')
    server = handlers_mod.build_mcp_server(lambda: fake_engine, apps_dir=tmp_path)
    async with Client(server) as client:
        tools = await client.list_tools()
        tool = next(t for t in tools.tools if t.name == 'run_dropper_pipe')
        assert tool.meta == {'ui': {'resourceUri': apps.DROPPER_URI}}
        listed = await client.list_resources()
        assert apps.DROPPER_URI in [str(r.uri) for r in listed.resources]
```

- [ ] **Step 2: Verify failure.** Step 3: Implement the three registrations (kwarg only on `run_dropper_pipe`'s existing register call — description/schema untouched; import `DROPPER_URI` from `..apps`).

- [ ] **Step 4: Build + suite**

```bash
./builder mcp-widgets:build && ls packages/ai/src/ai/modules/mcp/apps/dist/
python -m pytest tests/ai/modules/mcp/ -q -p no:cacheprovider   # from packages/ai
```
Expected: both `pipelines-table.html` and `dropper.html` present; suite green.

- [ ] **Step 5: Commit** — `feat(ai,mcp,mcp-widgets): dropper widget — in-chat upload + results`

---

### Task 6: E2E + docs

- [ ] **Step 1:** `./builder ai:test` (full env) — green.
- [ ] **Step 2:** doc.md "Embedded UI" section: add the dropper to the current-widgets paragraph, and one sentence documenting the CSP stamping (`connectDomains` = engine origin for widgets that upload). Same-change rule.
- [ ] **Step 3:** Append a "Slice 2 — dropper" section to `claude/tasks/mcp-apps-widgets/mcpjam-runbook.md`: run `run_dropper_pipe` in MCPJam → widget shows dropzone → drop a small text file → progress → results render; note the POST is synchronous so long pipelines hold the request open (browser default timeouts apply — flag if a real pipe exceeds ~5 min). Automated pre-check: extend the smoke script pattern to assert the dropper resource lists with CSP meta when `ROCKETRIDE_URI` is set. Mark browser steps pending for Dylan.
- [ ] **Step 4:** Commit — `docs(ai,mcp): dropper widget docs + runbook addendum`

## Known risks (write into the report if hit)

- **Pipe result field names** in `objects` are engine/pipe-dependent; `renderObject` uses the documented dropper-ui conventions (`mime_type` + base64 `data`, `text`) with JSON fallback for everything else. If a live sample (Task 6 manual pass) shows different field names, adjust `renderObject` only — the envelope contract is verified.
- **CSP enforcement differences per host**: MCPJam is permissive; Claude enforces declared domains strictly. The Task 3 stamping is the spec mechanism; if Claude still blocks the XHR, capture the console error verbatim — it's the key datum for a follow-up.
