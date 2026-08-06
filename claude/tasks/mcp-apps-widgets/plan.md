# MCP Apps Widgets — Slice 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unblock the mcp-v2 migration branch (uv override for the crewai/mcp conflict) and ship the MCP Apps plumbing plus the first embedded widget (pipelines table) on `feat/http-mcp`.

**Architecture:** A `depends.py` override mechanism lets the engine's combined dependency solve ignore crewai's `mcp<1.29` cap. On top of the unblocked branch, the MCP module gains `apps.py` (serves `ui://` single-file HTML resources, advertises the `io.modelcontextprotocol/ui` capability) and `ToolRegistry` gains per-tool `_meta.ui.resourceUri`. A new `apps/mcp-widgets` Vite workspace builds widget HTML bundles into the Python package.

**Tech Stack:** Python (`mcp>=2.0.0,<3`, low-level `Server`), uv (`--override`), TypeScript + Vite + `vite-plugin-singlefile` + `@modelcontextprotocol/ext-apps`, pytest with the in-memory `mcp.client.Client` harness.

## Global Constraints

- Branch: `feat/http-mcp`. Working dir note: the main checkout may be on another branch — check `git branch --show-current` first; if it isn't `feat/http-mcp`, coordinate with Dylan before switching, or work in a worktree.
- MIT license header on all new source files: `# Copyright 2026 Aparavi Software AG. MIT License.` (Python) / the block-comment form used in sibling files (TS/JS).
- Python: single quotes, ruff (`python -m ruff check`, `python -m ruff format`), Python 3.10+ compatible.
- TypeScript: single quotes, strict; ES2022 target.
- Conventional commits.
- The MCP module test suite must stay green: `packages/ai/tests/ai/modules/mcp/` (213 tests before this plan).
- Co-located docs rule: public-contract changes update `packages/ai/src/ai/modules/mcp/doc.md` in the same change.
- Never stage/commit anything under `pipelines/`.
- Test runner used below: `./builder ai:test` runs the whole ai suite through the real engine env (slow, needed for Task 1's dependency-solve verification). For fast iteration on module tests, the scratch pattern from 2026-07-30 works: a venv with `mcp>=2,<3`, `fastapi`, `python-dotenv`, `-e packages/client-python`, a no-op `depends.py` stub in site-packages, then `python -m pytest packages/ai/tests/ai/modules/mcp/ -q` from `packages/ai`. Task steps below say which runner they need; `pytest` in commands means the fast pattern is fine.

---

### Task 1: uv override support in depends.py (unblocks the branch)

**Files:**
- Modify: `packages/server/engine-lib/rocketlib-python/lib/depends.py` (near `REQUIREMENTS_GLOBS` line ~58, `_constraints_args` ~324, `_find_requirement_files` ~636, `_compile_constraints` ~687, `_install_dry_run` ~794, install ~900, `ensure_constraints` ~728)
- Create: `packages/ai/src/ai/overrides.txt`
- Modify: `claude/tasks/mcp-spec-2026-07-28-migration/open-questions.md` (record resolution)

**Interfaces:**
- Produces: `OVERRIDES_GLOBS` (module const), `_find_override_files() -> list[str]`, `_get_overrides_path() -> str`, `_override_args(exe_dir: str) -> list[str]`. All uv invocations (compile, dry-run, install) gain the override args; `ensure_constraints` hashes override files too.
- Note: `depends.py` has no pytest harness (engine-lib ships only `lib/` + `pip/`); verification is command-level — the baseline failure, a direct uv repro, and the full `./builder ai:test` gate.

- [ ] **Step 1: Reproduce the baseline failure**

Run: `./builder ai:test 2>&1 | tail -20`
Expected: FAIL — `RuntimeError: Failed to compile constraints` with `crewai ... depends on mcp>=1.26.0,<1.29` / `your requirements are unsatisfiable`. (If `node` is missing: `export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 22.22.3`.)

- [ ] **Step 2: Add override discovery + args to depends.py**

Below `REQUIREMENTS_GLOBS` (~line 62) add:

```python
# Override files: unlike constraints, uv overrides REPLACE what packages
# declare. Discovered like requirement files; see packages/ai/src/ai/overrides.txt
# for the policy comment. Named 'overrides.txt' so REQUIREMENTS_GLOBS
# ('requirement*.txt') never sweeps them into the combined requirements.
OVERRIDES_GLOBS = [
    'overrides.txt',
    'nodes/**/overrides.txt',
    'ai/**/overrides.txt',
]
```

Below `_constraints_args` (~line 332) add:

```python
def _get_overrides_path() -> str:
    """Path of the combined overrides file in the engine cache."""
    return os.path.join(engine_cache_dir(), 'overrides-combined.txt')


def _override_args(exe_dir: str) -> list[str]:
    """Return uv ``--override`` args if the combined overrides file is non-empty, else ``[]``.

    Relative to exe_dir (the subprocess cwd) — uv splits the value on whitespace.
    """
    overrides_path = _get_overrides_path()
    if os.path.exists(overrides_path) and os.path.getsize(overrides_path) > 0:
        return ['--override', os.path.relpath(overrides_path, exe_dir)]
    return []
```

Below `_find_requirement_files` (~line 650) add:

```python
def _find_override_files() -> list[str]:
    """Find all override files matching OVERRIDES_GLOBS."""
    executable_dir = _get_executable_dir()
    found = []
    for pattern in OVERRIDES_GLOBS:
        full_pattern = os.path.join(executable_dir, pattern)
        for path in glob(full_pattern, recursive=True):
            abs_path = os.path.abspath(path)
            if os.path.isfile(abs_path) and abs_path not in found:
                found.append(abs_path)
    return found
```

- [ ] **Step 3: Wire overrides into the three uv invocations**

In `_compile_constraints`, after the `args = [...]` list (before `debug(f'Compile: {args}')`):

```python
    args.extend(_override_args(exe_dir))
```

In `_install_dry_run`, directly after `args.extend(_constraints_args(constraints_path, exe_dir))`:

```python
    args.extend(_override_args(exe_dir))
```

In the install function (~line 925), directly after `uv_args.extend(_constraints_args(constraints_path, exe_dir))`:

```python
    uv_args.extend(_override_args(exe_dir))
```

- [ ] **Step 4: Hash + combine override files in ensure_constraints**

In `ensure_constraints`, change:

```python
    req_files = _find_requirement_files()
```
to:
```python
    req_files = _find_requirement_files()
    override_files = _find_override_files()
```

Change `current_hash = _compute_hash(req_files)` to:

```python
    current_hash = _compute_hash(req_files + override_files)
```

Directly before `_compile_constraints(constraints_path)` add (reuses the existing combiner; an empty file list yields a zero-byte file, which `_override_args` treats as absent):

```python
    _combine_requirements(override_files, _get_overrides_path())
```

- [ ] **Step 5: Create the override file**

Create `packages/ai/src/ai/overrides.txt` (the `ai:build` syncDir carries everything under `src/ai/` into dist, where the `ai/**/overrides.txt` glob finds it):

```
# uv resolution overrides (discovered by depends.py OVERRIDES_GLOBS).
# Unlike constraints, overrides REPLACE what packages declare. Only list a
# package here when we deliberately know better than a dependency's metadata.
#
# crewai (nodes/agent_crewai) caps mcp at <1.29 while our MCP server implements
# the 2026-07-28 spec (mcp>=2). agent_crewai never uses crewai's MCP-tool
# features, and crewai imports the mcp lib lazily (only when an agent config
# declares MCP tools), so forcing mcp v2 is safe for our usage.
# Remove this line once crewai releases mcp>=2 support.
mcp>=2.0.0,<3
```

- [ ] **Step 6: Direct uv sanity check of the exact mechanism**

```bash
S=$(mktemp -d); printf 'crewai>=1.14.1\nmcp>=2.0.0,<3\n' > $S/reqs.txt; printf 'mcp>=2.0.0,<3\n' > $S/ov.txt
~/.local/bin/uv pip compile $S/reqs.txt --override $S/ov.txt --index-strategy unsafe-best-match -o $S/out.txt --quiet && grep -E '^(mcp|crewai)==' $S/out.txt
```
Expected: PASS — prints a `crewai==` line and `mcp==2.x` line.

- [ ] **Step 7: Full builder gate**

Run: `./builder ai:test 2>&1 | tail -15`
Expected: PASS — constraints compile, install proceeds, ai suite green (MCP module: 213 passed). This is the step that proves the unblock end-to-end; do not substitute the fast pytest pattern here.

Note: this also clears the migration plan's "deployment gotcha" — the stale `dist/server/cache/constraints.txt` pinning `mcp==1.28.1` regenerates automatically here, because `ensure_constraints` hashes requirement+override files and both changed.

- [ ] **Step 8: Record the resolution + ruff + commit**

Append to `claude/tasks/mcp-spec-2026-07-28-migration/open-questions.md`:

```markdown
8. ~~**crewai constraint conflict**~~ — RESOLVED 2026-07-30: `depends.py` now supports uv
   `--override` files (`OVERRIDES_GLOBS`); `packages/ai/src/ai/overrides.txt` forces
   `mcp>=2.0.0,<3` past crewai's `<1.29` cap. Safe: agent_crewai never touches crewai's
   MCP features (lazy import, verified). Remove when crewai ships mcp-2 support.
```

```bash
python -m ruff check packages/server/engine-lib/rocketlib-python/lib/depends.py
git add packages/server/engine-lib/rocketlib-python/lib/depends.py packages/ai/src/ai/overrides.txt claude/tasks/mcp-spec-2026-07-28-migration/open-questions.md
git commit -m 'fix(engine,deps): uv override support; force mcp>=2 past crewai cap'
```

---

### Task 2: ToolRegistry emits `_meta.ui.resourceUri`

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/tooling.py`
- Create: `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Consumes: existing `ToolRegistry.register(name, description, schema)` decorator factory and `_ToolEntry` dataclass in `tooling.py`.
- Produces: `ToolRegistry.register(name, description, schema, *, ui_resource_uri: Optional[str] = None)`; `tools()` returns `types.Tool` with `meta={'ui': {'resourceUri': ...}}` when set, `meta=None` otherwise. (SDK v2 `Tool.meta` serializes on the wire as `_meta` — verified with `model_dump(by_alias=True)`.)

- [ ] **Step 1: Write the failing test**

Create `packages/ai/tests/ai/modules/mcp/test_apps.py`:

```python
# Copyright 2026 Aparavi Software AG. MIT License.
"""Contract tests for MCP Apps (embedded UI) plumbing."""

import mcp.types as types

from ai.modules.mcp.tooling import ToolRegistry


def _dummy_schema():
    return {'type': 'object', 'properties': {}}


def test_registry_emits_ui_meta_only_when_linked():
    registry = ToolRegistry()

    @registry.register('plain_tool', 'no ui', _dummy_schema())
    async def _plain(client, tasks, args):
        return {}

    @registry.register(
        'ui_tool', 'has ui', _dummy_schema(), ui_resource_uri='ui://rocketride/x.html'
    )
    async def _ui(client, tasks, args):
        return {}

    tools = {t.name: t for t in registry.tools()}
    assert tools['plain_tool'].meta is None
    assert tools['ui_tool'].meta == {'ui': {'resourceUri': 'ui://rocketride/x.html'}}
    # The wire field must be _meta (host compatibility).
    dumped = tools['ui_tool'].model_dump(by_alias=True, exclude_none=True)
    assert dumped['_meta'] == {'ui': {'resourceUri': 'ui://rocketride/x.html'}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ai/modules/mcp/test_apps.py -q` (from `packages/ai`, fast pattern)
Expected: FAIL — `TypeError: register() got an unexpected keyword argument 'ui_resource_uri'`

- [ ] **Step 3: Implement**

In `tooling.py`: add `ui_resource_uri: Optional[str] = None` to the `_ToolEntry` dataclass, then:

```python
    def register(
        self,
        name: str,
        description: str,
        schema: dict,
        *,
        ui_resource_uri: Optional[str] = None,
    ) -> Callable[[Callable], Callable]:
        """Return a decorator that registers ``fn`` as the handler for ``name``.

        ``ui_resource_uri`` links the tool to an MCP Apps widget (emitted as
        ``_meta.ui.resourceUri``; see apps.py). Hosts without the UI extension
        ignore it.
        """

        def _decorator(fn: Callable) -> Callable:
            self._entries[name] = _ToolEntry(
                description=description,
                schema=schema,
                handler=fn,
                ui_resource_uri=ui_resource_uri,
            )
            return fn

        return _decorator

    def tools(self) -> List[types.Tool]:
        """Return the registered tools as MCP ``types.Tool`` descriptors."""
        return [
            types.Tool(
                name=name,
                description=entry.description,
                input_schema=entry.schema,
                meta=(
                    {'ui': {'resourceUri': entry.ui_resource_uri}}
                    if entry.ui_resource_uri
                    else None
                ),
            )
            for name, entry in self._entries.items()
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ai/modules/mcp/ -q`
Expected: PASS (213 + 1).

- [ ] **Step 5: Commit**

```bash
python -m ruff check packages/ai/src/ai/modules/mcp/tooling.py packages/ai/tests/ai/modules/mcp/test_apps.py && python -m ruff format --check packages/ai/src/ai/modules/mcp/tooling.py packages/ai/tests/ai/modules/mcp/test_apps.py
git add packages/ai/src/ai/modules/mcp/tooling.py packages/ai/tests/ai/modules/mcp/test_apps.py
git commit -m 'feat(ai,mcp): ToolRegistry ui_resource_uri -> _meta.ui.resourceUri'
```

---

### Task 3: apps.py — ui:// resources + extension capability

**Files:**
- Create: `packages/ai/src/ai/modules/mcp/apps.py`
- Modify: `packages/ai/src/ai/modules/mcp/cache_policy.py` (add `UI_READ_TTL_MS`)
- Modify: `packages/ai/src/ai/modules/mcp/handlers.py` (`build_mcp_server`)
- Modify: `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Consumes: `build_mcp_server(engine_factory, task_registry=None, *, registry=None)` from Task 2's `ToolRegistry`; the in-memory `Client(server)` test harness and `fake_engine` fixture used across `test_handlers.py`.
- Produces (in `apps.py`): `UI_MIME_TYPE = 'text/html;profile=mcp-app'`; `UI_EXTENSION_ID = 'io.modelcontextprotocol/ui'`; `PIPELINES_TABLE_URI = 'ui://rocketride/pipelines-table.html'`; `AppSpec(uri, filename, title)` frozen dataclass; `available_apps(apps_dir: Optional[Path] = None) -> List[AppSpec]`; `list_ui_resources(apps_dir=None) -> List[types.Resource]`; `read_ui_resource(uri: str, apps_dir=None) -> Optional[str]`. `build_mcp_server` gains keyword-only `apps_dir: Optional[Path] = None` (test seam, like `registry`).

- [ ] **Step 1: Write the failing tests**

Append to `test_apps.py`:

```python
import json
from pathlib import Path

import pytest
from mcp.client import Client

from ai.modules.mcp import apps


@pytest.fixture
def apps_dir(tmp_path):
    (tmp_path / 'pipelines-table.html').write_text(
        '<!doctype html><html><body>widget</body></html>', encoding='utf-8'
    )
    return tmp_path


@pytest.mark.asyncio
async def test_ui_resource_listed_and_served(fake_engine, apps_dir):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine, apps_dir=apps_dir)
    async with Client(server) as client:
        listed = await client.list_resources()
        uris = [str(r.uri) for r in listed.resources]
        assert apps.PIPELINES_TABLE_URI in uris
        ui_res = next(r for r in listed.resources if str(r.uri) == apps.PIPELINES_TABLE_URI)
        assert ui_res.mime_type == apps.UI_MIME_TYPE

        read = await client.read_resource(apps.PIPELINES_TABLE_URI)
        assert read.contents[0].mime_type == apps.UI_MIME_TYPE
        assert 'widget' in read.contents[0].text


@pytest.mark.asyncio
async def test_no_widget_bundle_means_no_ui_surface(fake_engine, tmp_path):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine, apps_dir=tmp_path)
    assert apps.UI_EXTENSION_ID not in server.extensions
    async with Client(server) as client:
        listed = await client.list_resources()
        assert apps.PIPELINES_TABLE_URI not in [str(r.uri) for r in listed.resources]


def test_extension_capability_declared_when_widget_exists(fake_engine, apps_dir):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine, apps_dir=apps_dir)
    assert server.extensions[apps.UI_EXTENSION_ID] == {'mimeTypes': [apps.UI_MIME_TYPE]}
```

(If `read.contents[0].mime_type` fails as an attribute, the harness's existing
resource tests in `test_handlers.py` show the accessor the SDK exposes — mirror
that; the assertion's substance is the `text/html;profile=mcp-app` value.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/ai/modules/mcp/test_apps.py -q`
Expected: FAIL — `ImportError: cannot import name 'apps'` (module doesn't exist yet).

- [ ] **Step 3: Create apps.py**

```python
# Copyright 2026 Aparavi Software AG. MIT License.
"""MCP Apps (io.modelcontextprotocol/ui): embedded widget resources.

Widgets are single-file HTML bundles built from apps/mcp-widgets by
``builder mcp-widgets:build`` into ``apps/dist/`` next to this module (the
ai:build syncDir carries them into the server dist). Each widget is served as
a ``ui://`` resource with the profile mimeType; tools opt in by registering
with ``ui_resource_uri`` (see tooling.py). Hosts without the UI extension see
the plain JSON tool results, unchanged.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import mcp.types as types

UI_MIME_TYPE = 'text/html;profile=mcp-app'
UI_EXTENSION_ID = 'io.modelcontextprotocol/ui'

PIPELINES_TABLE_URI = 'ui://rocketride/pipelines-table.html'

_APPS_DIST = Path(__file__).parent / 'apps' / 'dist'


@dataclass(frozen=True)
class AppSpec:
    uri: str
    filename: str
    title: str


APPS: List[AppSpec] = [
    AppSpec(
        uri=PIPELINES_TABLE_URI,
        filename='pipelines-table.html',
        title='Running pipelines',
    ),
]


def extension_capability() -> dict:
    return {'mimeTypes': [UI_MIME_TYPE]}


def available_apps(apps_dir: Optional[Path] = None) -> List[AppSpec]:
    """Specs whose built HTML bundle actually exists on disk."""
    base = apps_dir if apps_dir is not None else _APPS_DIST
    return [spec for spec in APPS if (base / spec.filename).is_file()]


def list_ui_resources(apps_dir: Optional[Path] = None) -> List[types.Resource]:
    return [
        types.Resource(uri=spec.uri, name=spec.title, mimeType=UI_MIME_TYPE)
        for spec in available_apps(apps_dir)
    ]


def read_ui_resource(uri: str, apps_dir: Optional[Path] = None) -> Optional[str]:
    """Return the widget HTML for ``uri``, or None if unknown/not built."""
    base = apps_dir if apps_dir is not None else _APPS_DIST
    for spec in APPS:
        if spec.uri == uri and (base / spec.filename).is_file():
            return (base / spec.filename).read_text(encoding='utf-8')
    return None
```

- [ ] **Step 4: Add the cache hint**

In `cache_policy.py` add (widget HTML is static per build):

```python
UI_READ_TTL_MS = 3_600_000
```

- [ ] **Step 5: Wire into build_mcp_server**

In `handlers.py`: import `Path`/`Optional` as needed, `from . import apps as apps_mod`, and `UI_READ_TTL_MS` from `cache_policy`. Add the keyword-only param `apps_dir: Optional[Path] = None` to `build_mcp_server` (documented as a test seam like `registry`). Then:

In `_on_list_resources`, change the resources line to:

```python
        return types.ListResourcesResult(
            resources=resources_mod.list_resources() + apps_mod.list_ui_resources(apps_dir),
            ttl_ms=RESOURCES_LIST_TTL_MS,
            cache_scope=CACHE_SCOPE,
        )
```

In `_on_read_resource`, before the `rocketride://` handling:

```python
        ui_html = apps_mod.read_ui_resource(uri_str, apps_dir)
        if ui_html is not None:
            return types.ReadResourceResult(
                contents=[
                    types.TextResourceContents(
                        uri=params.uri, mimeType=apps_mod.UI_MIME_TYPE, text=ui_html
                    )
                ],
                ttl_ms=UI_READ_TTL_MS,
                cache_scope=CACHE_SCOPE,
            )
```

After the `server = Server(...)` construction, before `return server`:

```python
    if apps_mod.available_apps(apps_dir):
        server.extensions[apps_mod.UI_EXTENSION_ID] = apps_mod.extension_capability()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/ai/modules/mcp/ -q`
Expected: PASS (all module tests, including the three new ones).

- [ ] **Step 7: Commit**

```bash
python -m ruff check packages/ai/src/ai/modules/mcp/ && python -m ruff format --check packages/ai/src/ai/modules/mcp/apps.py
git add packages/ai/src/ai/modules/mcp/apps.py packages/ai/src/ai/modules/mcp/cache_policy.py packages/ai/src/ai/modules/mcp/handlers.py packages/ai/tests/ai/modules/mcp/test_apps.py
git commit -m 'feat(ai,mcp): serve ui:// widget resources + UI extension capability'
```

---

### Task 4: Link `list_running_pipelines` to the widget

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/tools/visibility.py` (the `list_running_pipelines` registration)
- Modify: `packages/ai/tests/ai/modules/mcp/test_apps.py`

**Interfaces:**
- Consumes: `apps.PIPELINES_TABLE_URI`, Task 2's `ui_resource_uri=` kwarg.
- Produces: `list_running_pipelines` carries `_meta.ui.resourceUri == 'ui://rocketride/pipelines-table.html'` in `tools/list`.

- [ ] **Step 1: Write the failing test**

Append to `test_apps.py`:

```python
@pytest.mark.asyncio
async def test_list_running_pipelines_links_widget(fake_engine, apps_dir):
    import ai.modules.mcp.handlers as handlers_mod

    server = handlers_mod.build_mcp_server(lambda: fake_engine, apps_dir=apps_dir)
    async with Client(server) as client:
        tools = await client.list_tools()
        tool = next(t for t in tools.tools if t.name == 'list_running_pipelines')
        assert tool.meta == {'ui': {'resourceUri': apps.PIPELINES_TABLE_URI}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/ai/modules/mcp/test_apps.py -q`
Expected: FAIL — `tool.meta` is `None`.

- [ ] **Step 3: Implement**

In `tools/visibility.py`, add the import `from ..apps import PIPELINES_TABLE_URI` and extend the existing registration decorator for `list_running_pipelines` with the kwarg:

```python
@registry.register(
    'list_running_pipelines',
    <existing description unchanged>,
    <existing schema unchanged>,
    ui_resource_uri=PIPELINES_TABLE_URI,
)
```

(Keep the existing description/schema arguments exactly as they are — only add the kwarg.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ai/modules/mcp/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
python -m ruff check packages/ai/src/ai/modules/mcp/tools/visibility.py
git add packages/ai/src/ai/modules/mcp/tools/visibility.py packages/ai/tests/ai/modules/mcp/test_apps.py
git commit -m 'feat(ai,mcp): link list_running_pipelines to pipelines-table widget'
```

---

### Task 5: apps/mcp-widgets workspace + pipelines-table widget

**Files:**
- Create: `apps/mcp-widgets/package.json`, `apps/mcp-widgets/tsconfig.json`, `apps/mcp-widgets/vite.config.ts`, `apps/mcp-widgets/src/pipelines-table/index.html`, `apps/mcp-widgets/src/pipelines-table/main.ts`, `apps/mcp-widgets/scripts/tasks.js`
- Modify: `pnpm-workspace.yaml` (add `apps/mcp-widgets` under `# Applications`), `.gitignore` (add `packages/ai/src/ai/modules/mcp/apps/dist/`)

**Interfaces:**
- Consumes: `@modelcontextprotocol/ext-apps` (`App` class: `connect()`, `ontoolresult`, `callServerTool({name, arguments})`); builder auto-discovery of `scripts/tasks.js` (see `apps/dropper-ui/scripts/tasks.js` for the module shape and `scripts/lib` helpers).
- Produces: `builder mcp-widgets:build` emits `packages/ai/src/ai/modules/mcp/apps/dist/pipelines-table.html` (single self-contained file, the exact filename `apps.py` declares).

- [ ] **Step 1: Scaffold the package**

`apps/mcp-widgets/package.json`:

```json
{
	"name": "@rocketride/mcp-widgets",
	"version": "0.1.0",
	"private": true,
	"description": "Embedded MCP Apps widgets served by the RocketRide HTTP MCP server",
	"license": "MIT",
	"type": "module",
	"scripts": {
		"build": "vite build",
		"typecheck": "tsc --noEmit"
	},
	"dependencies": {
		"@modelcontextprotocol/ext-apps": "^1.7.0"
	},
	"devDependencies": {
		"typescript": "^5.5.0",
		"vite": "^6.0.0",
		"vite-plugin-singlefile": "^2.0.0"
	}
}
```

(Version verified 2026-07-30: npm latest is 1.7.5; the API used below was confirmed against its `app.d.ts` — `App`, `connect`, `ontoolresult`, `callServerTool`, plus `sendMessage`/`requestDisplayMode`/`onhostcontextchanged` for later slices.)

`apps/mcp-widgets/tsconfig.json`:

```json
{
	"compilerOptions": {
		"target": "ES2022",
		"module": "ESNext",
		"moduleResolution": "bundler",
		"strict": true,
		"noEmit": true,
		"lib": ["ES2022", "DOM"]
	},
	"include": ["src"]
}
```

`apps/mcp-widgets/vite.config.ts`:

```ts
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 */
import { resolve } from 'path';
import { defineConfig } from 'vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// One entry per widget. Each must bundle to ONE self-contained HTML file
// (MCP Apps MVP forbids external URLs), so we build entries one at a time
// via the WIDGET env var rather than rollup multi-input.
const widget = process.env.WIDGET ?? 'pipelines-table';

export default defineConfig({
	root: resolve(__dirname, 'src', widget),
	plugins: [viteSingleFile()],
	build: {
		outDir: resolve(__dirname, 'dist'),
		emptyOutDir: false,
		rollupOptions: {
			input: resolve(__dirname, 'src', widget, 'index.html'),
			output: { entryFileNames: `${widget}.js` },
		},
	},
});
```

- [ ] **Step 2: Write the widget**

`apps/mcp-widgets/src/pipelines-table/index.html`:

```html
<!doctype html>
<html>
	<head>
		<meta charset="utf-8" />
		<title>Running pipelines</title>
		<style>
			:root { color-scheme: light dark; }
			body { font: 13px/1.5 system-ui, sans-serif; margin: 12px; }
			table { border-collapse: collapse; width: 100%; }
			th, td { text-align: left; padding: 4px 10px 4px 0; border-bottom: 1px solid color-mix(in srgb, currentColor 20%, transparent); }
			th { font-weight: 600; opacity: 0.7; }
			button { font: inherit; cursor: pointer; }
			.empty { opacity: 0.6; padding: 8px 0; }
		</style>
	</head>
	<body>
		<div id="root" class="empty">Waiting for pipeline data…</div>
		<script type="module" src="./main.ts"></script>
	</body>
</html>
```

`apps/mcp-widgets/src/pipelines-table/main.ts`:

```ts
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 *
 * Pipelines-table widget: renders the list_running_pipelines tool result and
 * offers refresh/terminate via bridge tool calls. Data contract (verified
 * against tools/visibility.py and the conftest contract fixture): the tool
 * returns JSON text content shaped { ok, tasks: [...], count } where each
 * task row has { token, name, description? } (state is NOT in the row —
 * per-task state needs get_task_status and stays out of slice 1).
 */
import { App } from '@modelcontextprotocol/ext-apps';

interface TaskRow {
	token: string;
	name: string;
	description?: string;
}

const app = new App({ name: 'RocketRide pipelines table', version: '0.1.0' });
const root = document.getElementById('root') as HTMLElement;

function parseRows(result: unknown): TaskRow[] {
	// Tool results arrive as [{ type: 'text', text: '<json>' }].
	const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
	const text = content.find((c) => c.type === 'text')?.text;
	if (!text) return [];
	try {
		const payload = JSON.parse(text) as { tasks?: TaskRow[] };
		return payload.tasks ?? [];
	} catch {
		return [];
	}
}

function render(rows: TaskRow[]): void {
	root.classList.remove('empty');
	if (rows.length === 0) {
		root.classList.add('empty');
		root.textContent = 'No pipelines running.';
		return;
	}
	const table = document.createElement('table');
	table.innerHTML =
		'<thead><tr><th>Name</th><th>Description</th><th>Token</th><th></th></tr></thead>';
	const tbody = document.createElement('tbody');
	for (const row of rows) {
		const tr = document.createElement('tr');
		const cells = [row.name, row.description ?? '', row.token].map((v) => {
			const td = document.createElement('td');
			td.textContent = v;
			return td;
		});
		const actions = document.createElement('td');
		const stop = document.createElement('button');
		stop.textContent = 'Terminate';
		stop.onclick = async () => {
			stop.disabled = true;
			// terminate's schema requires task_token (see execution.py _TERMINATE_SCHEMA).
			await app.callServerTool({ name: 'terminate', arguments: { task_token: row.token } });
			await refresh();
		};
		actions.appendChild(stop);
		tr.append(...cells, actions);
		tbody.appendChild(tr);
	}
	table.appendChild(tbody);
	root.replaceChildren(table);

	const reload = document.createElement('button');
	reload.textContent = 'Refresh';
	reload.onclick = refresh;
	root.appendChild(reload);
}

async function refresh(): Promise<void> {
	const result = await app.callServerTool({ name: 'list_running_pipelines', arguments: {} });
	render(parseRows(result));
}

// Initial data: the host pushes the tool result that triggered this widget.
app.ontoolresult = (result) => render(parseRows(result));
app.connect();
```

(Contract verified 2026-07-30 against `tools/visibility.py`: the handler returns `{'ok': True, 'tasks': running, 'count': len(running)}` where `running` is the engine's `rrext_get_tasks` list; the module's contract fixture (`conftest.py`) shows row fields `{'name', 'description', 'token'}`. `terminate` requires `task_token`. The code above already matches — no adjustment step needed.)

- [ ] **Step 3: Builder task**

`apps/mcp-widgets/scripts/tasks.js` — model on `apps/dropper-ui/scripts/tasks.js` (same header, same `scripts/lib` imports). Key differences, concretely:

```js
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 */

/**
 * MCP Widgets Build Module
 *
 * Single-file HTML widgets (MCP Apps) served by the ai MCP module.
 */
const path = require('path');
const { execCommand, syncDir, formatSyncStats, removeDir } = require('../../../scripts/lib');
const { PROJECT_ROOT } = require('../../../scripts/lib/paths');

const APP_ROOT = path.join(__dirname, '..');
const DIST_DIR = path.join(APP_ROOT, 'dist');
const AI_APPS_DIST = path.join(
	PROJECT_ROOT, 'packages', 'ai', 'src', 'ai', 'modules', 'mcp', 'apps', 'dist'
);
const WIDGETS = ['pipelines-table'];

function makeBundleAction() {
	return {
		run: async (ctx, task) => {
			for (const widget of WIDGETS) {
				task.output = `Building ${widget}...`;
				await execCommand('pnpm', ['run', 'build'], {
					cwd: APP_ROOT,
					env: { ...process.env, WIDGET: widget },
				});
			}
			const stats = await syncDir(DIST_DIR, AI_APPS_DIST, { mirror: true });
			task.output = formatSyncStats(stats);
		},
	};
}

function makeCleanAction() {
	return {
		run: async () => {
			await removeDir(DIST_DIR);
			await removeDir(AI_APPS_DIST);
		},
	};
}

module.exports = {
	name: 'mcp-widgets',
	tasks: [
		{ name: 'mcp-widgets:build', action: makeBundleAction },
		{ name: 'mcp-widgets:clean', action: makeCleanAction },
	],
};
```

(Check the exact `module.exports` shape against `apps/dropper-ui/scripts/tasks.js` — mirror its structure for task registration, caching helpers, and any required fields like descriptions; the code above shows the actions, dropper-ui shows the envelope. If dropper-ui gates on `hasBuildInputChanged`/`saveSourceHash`, replicate that with `BUILD_HASH_KEY = 'mcp-widgets.buildHash'`.)

Also: add `apps/mcp-widgets` to `pnpm-workspace.yaml` under `# Applications` (alphabetical: after `apps/hello-ui`), and append `packages/ai/src/ai/modules/mcp/apps/dist/` to `.gitignore`.

- [ ] **Step 4: Install + typecheck + build**

```bash
pnpm install
cd apps/mcp-widgets && pnpm run typecheck && cd ../..
./builder mcp-widgets:build
ls packages/ai/src/ai/modules/mcp/apps/dist/
```
Expected: `pipelines-table.html` exists; `grep -c '<script' packages/ai/src/ai/modules/mcp/apps/dist/pipelines-table.html` ≥ 1 and `grep -c 'src=' ...` on script tags shows no external `src` (single-file: JS is inlined).

- [ ] **Step 5: Commit**

```bash
git add apps/mcp-widgets pnpm-workspace.yaml .gitignore pnpm-lock.yaml
git commit -m 'feat(mcp-widgets): pipelines-table widget workspace + builder task'
```

---

### Task 6: End-to-end verification + docs

**Files:**
- Modify: `packages/ai/src/ai/modules/mcp/doc.md` (new "Embedded UI (MCP Apps)" section)
- Create: `claude/tasks/mcp-apps-widgets/mcpjam-runbook.md`

**Interfaces:**
- Consumes: everything above; `MCP_DEV_NO_AUTH=1` dev bypass (see `packages/ai/src/ai/modules/mcp/__init__.py`).

- [ ] **Step 1: Full suite through the real builder**

Run: `./builder ai:test 2>&1 | tail -10`
Expected: PASS — this exercises the depends override AND the module tests in the real env.

- [ ] **Step 2: Update doc.md**

Add a section to `packages/ai/src/ai/modules/mcp/doc.md` after the resources section:

```markdown
## Embedded UI (MCP Apps)

The server implements the `io.modelcontextprotocol/ui` extension (spec
2026-01-26). Widgets are single-file HTML bundles built by
`builder mcp-widgets:build` from `apps/mcp-widgets/` into `apps/dist/` next to
this module, served as `ui://rocketride/<name>.html` resources with mimeType
`text/html;profile=mcp-app`. A tool opts in via
`ToolRegistry.register(..., ui_resource_uri=...)`, which emits
`_meta.ui.resourceUri`; hosts that support MCP Apps render the widget beside
that tool's result, all other hosts see the unchanged JSON. The capability is
advertised only when at least one built bundle exists on disk.

Current widgets: `pipelines-table` (linked to `list_running_pipelines`;
refresh/terminate call back through the bridge).
```

- [ ] **Step 3: Write and execute the MCPJam runbook**

Create `claude/tasks/mcp-apps-widgets/mcpjam-runbook.md`:

```markdown
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
```

Execute it and record results in the file.

- [ ] **Step 4: Final commit**

```bash
git add packages/ai/src/ai/modules/mcp/doc.md claude/tasks/mcp-apps-widgets/
git commit -m 'docs(ai,mcp): document embedded UI surface + MCPJam runbook'
```

---

## Follow-up plans (not in this document)

- **Slice 2 — dropper widget** (`run_dropper_pipe`): ports dropper-ui's Views; needs the `csp.connectDomains` upload decision (open-questions #3).
- **Slice 3 — run monitor/trace** (`monitor` + `get_pipeline_trace` polling via `since` cursor).
- **Slice 4 — pipeline graph** (`describe_pipeline`/`validate_pipeline` DAG render).

Each gets its own plan after slice 1's MCPJam verification confirms real bridge behavior (tool-result push shape, sizing, theming).
