# MCP Apps Widgets — Slice 3: Run Monitor / Trace Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live run-monitor widget attached to `monitor` and `get_pipeline_trace`: status header + per-node trace feed that keeps polling `get_pipeline_trace` through the bridge until the task is terminal.

**Architecture:** One widget serves both tools (both registrations point at the same `ui://` resource). On push it renders whichever result shape it got (a `monitor` snapshot or a trace page), then enters a poll loop: every 2.5s `callServerTool('get_pipeline_trace', {task_token, since: cursor})` for new events plus a `monitor` call with a short timeout for the status header; stops on `terminal: true`, on teardown, or after 10 consecutive empty/error polls. All bridge calls go through the host's consent path — hosts may prompt on the first widget-initiated call.

**Tech Stack:** as slice 2 (vanilla TS widget; no server-side changes beyond registration).

## Global Constraints

Same as `plan-slice2-dropper.md` Global Constraints (branch/base, headers, quotes, fast test runner, builder/nvm/codesign notes, single-file bundles, never commit `pipelines/`). Prerequisite: slice 2's Task 3 (CSP stamping) is NOT needed here — this widget makes no direct network calls, only bridge tool calls.

## Verified contracts (scouted 2026-08-03)

- `monitor` result = `_snapshot` (visibility.py:96-115): `{ok, task_token, state: int, state_label, completed, terminal, status: <raw dict>, counts: {completedCount, failedCount, totalCount}, errors: [], warnings: [], polls}`. State ints: 0 none, 1 starting, 2 initializing, 3 running, 4 stopping, 5 completed, 6 cancelled.
- `monitor` input schema includes `task_token` (required) and a wall-clock `timeout` — pass a SHORT timeout (e.g. 5) from the widget so the bounded poll returns promptly with a non-terminal snapshot instead of hanging the bridge call. (Confirm the exact timeout field name/units in `_MONITOR_SCHEMA` before coding — it's in visibility.py above the register block.)
- `get_pipeline_trace` result (visibility.py:196-209): `{ok, task_token, events: [...], cursor: int, count: int}` (+ optional `note` on first call). Page with `since` = previous `cursor`. Each event: `{seq: int, pipe: <component id>, op, pipes, trace, source}`.
- Trace requires the task to run with `pipelineTraceLevel` — without it the feed stays empty; the widget must say so rather than look broken.

---

### Task 1: The monitor widget

**Files:**
- Create: `apps/mcp-widgets/src/run-monitor/index.html`, `apps/mcp-widgets/src/run-monitor/main.ts`

- [ ] **Step 1: index.html** — same skeleton as the existing widgets; styles for a status header (`.status-chip` per-state colors via `color-mix`), a counts line, and a scrollable event feed (`.feed { max-height: 320px; overflow-y: auto; }`, `.event { font-family: ui-monospace, monospace; font-size: 12px; }`), grouped-by-pipe headers. Title `Run monitor`.

- [ ] **Step 2: main.ts** — complete implementation:

```ts
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 *
 * Run-monitor widget: linked to both `monitor` and `get_pipeline_trace`.
 * Renders the pushed result, then polls get_pipeline_trace (cursor paging via
 * `since`) and monitor (short timeout) until the task is terminal.
 */
import { App } from '@modelcontextprotocol/ext-apps';

interface Snapshot {
	ok: boolean;
	task_token: string;
	state: number;
	state_label: string;
	terminal: boolean;
	counts: { completedCount: number; failedCount: number; totalCount: number };
	errors: unknown[];
	warnings: unknown[];
}

interface TraceEvent {
	seq: number;
	pipe: string;
	op: string;
	trace?: unknown;
	source?: string;
}

interface TracePage {
	ok: boolean;
	task_token: string;
	events: TraceEvent[];
	cursor: number;
	note?: string;
}

const POLL_MS = 2500;
const MAX_IDLE_POLLS = 10;

const app = new App({ name: 'RocketRide run monitor', version: '0.1.0' });
const root = document.getElementById('root') as HTMLElement;

let token = '';
let cursor = 0;
let idlePolls = 0;
let stopped = false;
const events: TraceEvent[] = [];
let snapshot: Snapshot | null = null;

function parsePayload(result: unknown): Record<string, unknown> | null {
	const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
	const text = content.find((c) => c.type === 'text')?.text;
	if (!text) return null;
	try {
		return JSON.parse(text) as Record<string, unknown>;
	} catch {
		return null;
	}
}

function render(): void {
	root.classList.remove('empty');
	const wrap = document.createElement('div');
	if (snapshot) {
		const header = document.createElement('div');
		const chip = document.createElement('span');
		chip.className = `status-chip state-${snapshot.state}`;
		chip.textContent = snapshot.state_label;
		header.appendChild(chip);
		const counts = document.createElement('span');
		counts.textContent =
			` ${snapshot.counts.completedCount}/${snapshot.counts.totalCount} done` +
			(snapshot.counts.failedCount ? `, ${snapshot.counts.failedCount} failed` : '');
		header.appendChild(counts);
		wrap.appendChild(header);
		for (const err of snapshot.errors) {
			const line = document.createElement('p');
			line.className = 'error';
			line.textContent = String(err);
			wrap.appendChild(line);
		}
	}
	const feed = document.createElement('div');
	feed.className = 'feed';
	if (events.length === 0) {
		const hint = document.createElement('p');
		hint.className = 'empty';
		hint.textContent =
			'No trace events yet. (Trace needs the task started with pipelineTraceLevel.)';
		feed.appendChild(hint);
	}
	let lastPipe = '';
	for (const ev of events) {
		if (ev.pipe !== lastPipe) {
			lastPipe = ev.pipe;
			const h = document.createElement('h3');
			h.textContent = ev.pipe;
			feed.appendChild(h);
		}
		const line = document.createElement('div');
		line.className = 'event';
		line.textContent = `#${ev.seq} ${ev.op}${ev.trace ? ' ' + JSON.stringify(ev.trace) : ''}`;
		feed.appendChild(line);
	}
	wrap.appendChild(feed);
	if (stopped || snapshot?.terminal) {
		const done = document.createElement('p');
		done.textContent = snapshot?.terminal ? 'Run finished.' : 'Monitoring stopped.';
		wrap.appendChild(done);
	}
	root.replaceChildren(wrap);
	feed.scrollTop = feed.scrollHeight;
}

async function poll(): Promise<void> {
	if (stopped || !token) return;
	try {
		const traceResult = await app.callServerTool({
			name: 'get_pipeline_trace',
			arguments: { task_token: token, since: cursor },
		});
		const page = parsePayload(traceResult) as TracePage | null;
		if (page?.ok) {
			if (page.events.length) {
				events.push(...page.events);
				cursor = page.cursor;
				idlePolls = 0;
			} else {
				idlePolls += 1;
			}
		}
		const monResult = await app.callServerTool({
			name: 'monitor',
			arguments: { task_token: token, timeout: 5 },
		});
		const snap = parsePayload(monResult) as Snapshot | null;
		if (snap?.ok) snapshot = snap;
	} catch {
		idlePolls += 1;
	}
	if (snapshot?.terminal || idlePolls >= MAX_IDLE_POLLS) stopped = true;
	render();
	if (!stopped) setTimeout(poll, POLL_MS);
}

app.ontoolresult = (result) => {
	const payload = parsePayload(result);
	if (!payload || typeof payload.task_token !== 'string') {
		root.textContent = 'No task_token in the tool result.';
		return;
	}
	token = payload.task_token;
	if (Array.isArray(payload.events)) {
		events.push(...(payload.events as TraceEvent[]));
		cursor = typeof payload.cursor === 'number' ? payload.cursor : 0;
	} else if (typeof payload.state === 'number') {
		snapshot = payload as unknown as Snapshot;
	}
	render();
	if (!snapshot?.terminal) setTimeout(poll, POLL_MS);
};
app.connect();
```

(Before finalizing: open `visibility.py`, confirm `_MONITOR_SCHEMA`'s timeout field name and units, and adjust the `{ task_token: token, timeout: 5 }` arguments to match exactly.)

- [ ] **Step 3: Typecheck.** Commit with Task 2.

---

### Task 2: Register + link

**Files:**
- Modify: `apps/mcp-widgets/scripts/tasks.cjs` (`WIDGETS` += `'run-monitor'`), `packages/ai/src/ai/modules/mcp/apps.py` (`RUN_MONITOR_URI = 'ui://rocketride/run-monitor.html'`, spec `filename='run-monitor.html'`, `title='Run monitor'` — no `needs_engine_origin`), `packages/ai/src/ai/modules/mcp/tools/visibility.py` (add `ui_resource_uri=RUN_MONITOR_URI` kwarg to BOTH the `monitor` and `get_pipeline_trace` registrations; descriptions/schemas untouched), `packages/ai/tests/ai/modules/mcp/test_apps.py`

- [ ] **Step 1: Failing test** (append; write `run-monitor.html` into `tmp_path` like the dropper test):

```python
@pytest.mark.asyncio
async def test_monitor_tools_link_run_monitor_widget(fake_engine, tmp_path):
    import ai.modules.mcp.handlers as handlers_mod

    (tmp_path / 'run-monitor.html').write_text('<!doctype html><html><body>m</body></html>')
    server = handlers_mod.build_mcp_server(lambda: fake_engine, apps_dir=tmp_path)
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    expected = {'ui': {'resourceUri': apps.RUN_MONITOR_URI}}
    assert tools['monitor'].meta == expected
    assert tools['get_pipeline_trace'].meta == expected
```

- [ ] **Step 2: Verify failure → implement → suite green.**
- [ ] **Step 3: Build** — `./builder mcp-widgets:build`; all three HTML files present in `apps/dist/`.
- [ ] **Step 4: Commit** — `feat(ai,mcp,mcp-widgets): run-monitor widget on monitor + get_pipeline_trace`

---

### Task 3: E2E + docs

- [ ] **Step 1:** `./builder ai:test` green.
- [ ] **Step 2:** doc.md current-widgets paragraph += run-monitor (both tools, poll loop).
- [ ] **Step 3:** Runbook addendum "Slice 3 — run monitor": start a pipe with `pipelineTraceLevel='full'` via `run_pipeline`, run `monitor` in MCPJam → widget renders header, feed fills as data flows; verify the poll stops at terminal state; note hosts may prompt to approve widget-initiated tool calls (expected consent behavior, not a bug). Browser steps pending Dylan.
- [ ] **Step 4:** Commit — `docs(ai,mcp): run-monitor docs + runbook addendum`

## Known risks

- Widget-initiated `callServerTool` consent UX differs per host (MCPJam: silent; Claude: may prompt per call or per session). If Claude prompts on every 2.5s poll, that's a finding for the ledger — mitigation candidates (longer interval, manual refresh button) go into a follow-up, not speculative code now.
- `monitor`'s bounded poll holds the bridge call open up to `timeout` — keep it short (5s) so the widget stays responsive.
