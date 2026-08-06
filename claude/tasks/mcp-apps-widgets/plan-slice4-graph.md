# MCP Apps Widgets — Slice 4: Pipeline Graph Widget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render a pipeline's DAG in-chat from `describe_pipeline`, with `validate_pipeline` errors overlaid — pure-SVG layered layout, no graph libraries.

**Architecture:** One widget linked to both `describe_pipeline` and `validate_pipeline`. `describe_pipeline` returns components with a **verbatim** `inputs` field (the raw `.pipe` edge/lane encoding — not normalized by the server), so the widget owns edge parsing. Layout is a simple longest-path layering (topological levels → columns, nodes stacked per column, cubic-bezier edges), all generated DOM/SVG — no dependencies, keeps the bundle tiny. Validation errors render as a list; nodes whose id appears in an error string get highlighted (best-effort — engine error items are engine-defined and carry no structured component reference).

**Tech Stack:** as slice 2/3 (vanilla TS widget; server-side registration only).

## Global Constraints

Same as `plan-slice2-dropper.md` Global Constraints (branch/base, headers, quotes, fast test runner, builder/nvm/codesign, single-file bundles, never commit `pipelines/`).

## Verified contracts (scouted 2026-08-03)

- `describe_pipeline` (introspection.py:78-88): `{ok: True, source: <pipeline source>, components: [{id, provider, title, classType, inputs}]}` — `inputs` is verbatim `comp['input']` from the pipeline JSON.
- `validate_pipeline` (introspection.py:54-59): `{ok: <no errors>, errors: <engine list verbatim>, warnings: <verbatim>}` — same shape on success and failure; error items are NOT reshaped and have no structured component-id field.
- **The `input` encoding is the one thing this plan does not pin** — it must be locked from the schema source of truth in Task 1 before widget code is finalized.

---

### Task 1: Lock the edge encoding + parser

**Files:**
- Create: `apps/mcp-widgets/src/pipeline-graph/edges.ts`
- Create: `apps/mcp-widgets/src/pipeline-graph/edges.test.md` (a NOTES file, not a test runner — records the evidence; the workspace has no JS test harness and adding one for a parser this size is not warranted)

- [ ] **Step 1: Derive the encoding from the source of truth.** Read `packages/client-typescript/src/client/types/pipeline.ts` (the `.pipe` schema — find the component `input` field's type) AND cross-check against 2–3 real files in `examples/*.pipe` (e.g. `examples/tool-pipe-diamond.pipe` for multi-edge shapes). Record in `edges.test.md`: the exact type, 3 pasted real `input` values, and the parse rules derived. Do not guess — if the schema and examples disagree, the examples (engine-accepted files) win, note the discrepancy.

- [ ] **Step 2: Write the parser against what you found.** Expected shape (verify, adjust if Step 1 shows otherwise): `input` maps lane name → producer reference(s), where a reference names a component id (possibly `id:lane`). Parser contract:

```ts
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 *
 * Edge extraction from the verbatim `.pipe` component `input` field.
 * Encoding evidence: see edges.test.md (derived from
 * packages/client-typescript/src/client/types/pipeline.ts + examples/*.pipe).
 */

export interface Edge {
	from: string; // producer component id
	to: string; // consumer component id
	lane: string; // consumer's input lane name
}

export function parseEdges(
	components: Array<{ id: string; inputs?: unknown }>,
): Edge[] {
	const edges: Edge[] = [];
	for (const comp of components) {
		if (!comp.inputs || typeof comp.inputs !== 'object') continue;
		for (const [lane, ref] of Object.entries(comp.inputs as Record<string, unknown>)) {
			const refs = Array.isArray(ref) ? ref : [ref];
			for (const r of refs) {
				if (typeof r !== 'string') continue;
				const from = r.includes(':') ? r.slice(0, r.indexOf(':')) : r;
				edges.push({ from, to: comp.id, lane });
			}
		}
	}
	return edges;
}
```

Adjust field access to the real encoding from Step 1 (the plan's `describe_pipeline` contract calls the field `inputs` on the tool result even though the raw pipeline key is `input` — confirm which key the tool result actually uses by reading introspection.py:78-88 once more; the scout recorded `inputs`).

- [ ] **Step 3: Sanity-run the parser** against a real example: paste one example `.pipe`'s components into a small node eval (`node --input-type=module -e ...` or a scratch ts file run via vite-node if available; otherwise hand-trace in edges.test.md). Record input → edges output in `edges.test.md`.

- [ ] **Step 4: Commit** — `feat(mcp-widgets): pipeline edge parser + encoding evidence`

---

### Task 2: The graph widget

**Files:**
- Create: `apps/mcp-widgets/src/pipeline-graph/index.html`, `apps/mcp-widgets/src/pipeline-graph/main.ts`

- [ ] **Step 1: index.html** — standard skeleton; styles: `.node rect` fill via `color-mix(in srgb, currentColor 8%, transparent)`, `.node.error rect` red-tinted stroke, `.node text { font-size: 11px }`, `svg { width: 100%; height: auto; }`, `.errors { color: #c00; }` list below the graph. Title `Pipeline graph`.

- [ ] **Step 2: main.ts** — complete implementation:

```ts
/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 *
 * Pipeline-graph widget: linked to describe_pipeline and validate_pipeline.
 * Layered DAG layout (longest-path levels), pure SVG, no dependencies.
 */
import { App } from '@modelcontextprotocol/ext-apps';
import { parseEdges, type Edge } from './edges';

interface Component {
	id: string;
	provider: string;
	title?: string;
	classType?: string | string[];
	inputs?: unknown;
}

const NODE_W = 150;
const NODE_H = 44;
const GAP_X = 70;
const GAP_Y = 18;
const SVG_NS = 'http://www.w3.org/2000/svg';

const app = new App({ name: 'RocketRide pipeline graph', version: '0.1.0' });
const root = document.getElementById('root') as HTMLElement;

let components: Component[] = [];
let errors: unknown[] = [];
let warnings: unknown[] = [];

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

/** Longest-path layering: level(n) = 1 + max(level(producers)), cycles guarded. */
function layer(comps: Component[], edges: Edge[]): Map<string, number> {
	const producers = new Map<string, string[]>();
	for (const e of edges) {
		producers.set(e.to, [...(producers.get(e.to) ?? []), e.from]);
	}
	const levels = new Map<string, number>();
	const visiting = new Set<string>();
	const resolve = (id: string): number => {
		const known = levels.get(id);
		if (known !== undefined) return known;
		if (visiting.has(id)) return 0; // cycle guard: engine forbids cycles anyway
		visiting.add(id);
		const ps = producers.get(id) ?? [];
		const level = ps.length ? 1 + Math.max(...ps.map(resolve)) : 0;
		visiting.delete(id);
		levels.set(id, level);
		return level;
	};
	for (const c of comps) resolve(c.id);
	return levels;
}

function errorMentions(id: string): boolean {
	return errors.some((e) => JSON.stringify(e).includes(id));
}

function svgEl(tag: string): SVGElement {
	return document.createElementNS(SVG_NS, tag);
}

function render(): void {
	root.classList.remove('empty');
	if (components.length === 0) {
		root.textContent = 'No components to draw yet — run describe_pipeline.';
		return;
	}
	const edges = parseEdges(components);
	const levels = layer(components, edges);
	const columns = new Map<number, Component[]>();
	for (const c of components) {
		const lv = levels.get(c.id) ?? 0;
		columns.set(lv, [...(columns.get(lv) ?? []), c]);
	}
	const maxLevel = Math.max(...columns.keys());
	const maxRows = Math.max(...[...columns.values()].map((col) => col.length));
	const width = (maxLevel + 1) * (NODE_W + GAP_X) - GAP_X;
	const height = maxRows * (NODE_H + GAP_Y) - GAP_Y;

	const pos = new Map<string, { x: number; y: number }>();
	for (const [lv, col] of columns) {
		col.forEach((c, i) => {
			pos.set(c.id, { x: lv * (NODE_W + GAP_X), y: i * (NODE_H + GAP_Y) });
		});
	}

	const svg = svgEl('svg') as SVGSVGElement;
	svg.setAttribute('viewBox', `-4 -4 ${width + 8} ${height + 8}`);

	for (const e of edges) {
		const from = pos.get(e.from);
		const to = pos.get(e.to);
		if (!from || !to) continue;
		const x1 = from.x + NODE_W;
		const y1 = from.y + NODE_H / 2;
		const x2 = to.x;
		const y2 = to.y + NODE_H / 2;
		const mid = (x1 + x2) / 2;
		const path = svgEl('path');
		path.setAttribute('d', `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
		path.setAttribute('fill', 'none');
		path.setAttribute('stroke', 'currentColor');
		path.setAttribute('stroke-opacity', '0.35');
		const title = svgEl('title');
		title.textContent = e.lane;
		path.appendChild(title);
		svg.appendChild(path);
	}

	for (const c of components) {
		const p = pos.get(c.id);
		if (!p) continue;
		const g = svgEl('g');
		g.setAttribute('class', errorMentions(c.id) ? 'node error' : 'node');
		g.setAttribute('transform', `translate(${p.x}, ${p.y})`);
		const rect = svgEl('rect');
		rect.setAttribute('width', String(NODE_W));
		rect.setAttribute('height', String(NODE_H));
		rect.setAttribute('rx', '6');
		rect.setAttribute('stroke', 'currentColor');
		g.appendChild(rect);
		const label = svgEl('text');
		label.setAttribute('x', '8');
		label.setAttribute('y', '18');
		label.textContent = c.title || c.id;
		g.appendChild(label);
		const sub = svgEl('text');
		sub.setAttribute('x', '8');
		sub.setAttribute('y', '34');
		sub.setAttribute('opacity', '0.6');
		sub.textContent = c.provider;
		g.appendChild(sub);
		svg.appendChild(g);
	}

	const wrap = document.createElement('div');
	wrap.appendChild(svg);
	if (errors.length || warnings.length) {
		const list = document.createElement('ul');
		list.className = 'errors';
		for (const e of errors) {
			const li = document.createElement('li');
			li.textContent = typeof e === 'string' ? e : JSON.stringify(e);
			list.appendChild(li);
		}
		for (const w of warnings) {
			const li = document.createElement('li');
			li.style.opacity = '0.7';
			li.textContent = 'warning: ' + (typeof w === 'string' ? w : JSON.stringify(w));
			list.appendChild(li);
		}
		wrap.appendChild(list);
	}
	root.replaceChildren(wrap);
}

app.ontoolresult = (result) => {
	const payload = parsePayload(result);
	if (!payload) return;
	if (Array.isArray(payload.components)) {
		components = payload.components as Component[];
	}
	if (Array.isArray(payload.errors)) errors = payload.errors;
	if (Array.isArray(payload.warnings)) warnings = payload.warnings;
	render();
};
app.connect();
```

Note the two-tool behavior: a `describe_pipeline` push draws the graph; a `validate_pipeline` push (errors/warnings only, no components) renders the error list and highlights nothing until a describe has run in the same widget instance. That's acceptable v1 behavior — document it in the runbook.

- [ ] **Step 3: Typecheck.** Commit with Task 3.

---

### Task 3: Register + link

**Files:**
- Modify: `apps/mcp-widgets/scripts/tasks.cjs` (`WIDGETS` += `'pipeline-graph'`), `packages/ai/src/ai/modules/mcp/apps.py` (`PIPELINE_GRAPH_URI = 'ui://rocketride/pipeline-graph.html'`, spec `filename='pipeline-graph.html'`, `title='Pipeline graph'`), `packages/ai/src/ai/modules/mcp/tools/introspection.py` (add `ui_resource_uri=PIPELINE_GRAPH_URI` to BOTH `describe_pipeline` and `validate_pipeline` registrations), `packages/ai/tests/ai/modules/mcp/test_apps.py`

- [ ] **Step 1: Failing test** (same pattern as slice 3's link test — write `pipeline-graph.html` into `tmp_path`, assert both tools' `meta == {'ui': {'resourceUri': apps.PIPELINE_GRAPH_URI}}`).
- [ ] **Step 2: Implement → module suite green → `./builder mcp-widgets:build` shows all four bundles.**
- [ ] **Step 3: Commit** — `feat(ai,mcp,mcp-widgets): pipeline-graph widget on describe/validate`

---

### Task 4: E2E + docs

- [ ] **Step 1:** `./builder ai:test` green.
- [ ] **Step 2:** doc.md current-widgets paragraph += pipeline-graph.
- [ ] **Step 3:** Runbook addendum "Slice 4 — pipeline graph": in MCPJam run `describe_pipeline` on `examples/tool-pipe-diamond.pipe` (a shape with real fan-out/fan-in) → DAG renders with correct edges; run `validate_pipeline` on a deliberately broken copy → errors list renders. Browser steps pending Dylan.
- [ ] **Step 4:** Commit — `docs(ai,mcp): pipeline-graph docs + runbook addendum`

## Known risks

- The `input` encoding derivation (Task 1) is the load-bearing step; everything else is mechanical. If the encoding turns out to be richer than lane→refs (e.g. objects with options), `parseEdges` absorbs it — keep the `Edge` interface stable so `main.ts` doesn't change.
- Error→node mapping is substring-based by design (engine errors carry no structured component ref). If it proves too noisy in practice, drop the highlight and keep the list — don't build an error-parser.
- Very wide pipelines will render small; `viewBox` scaling handles it, and MCPJam/Claude fullscreen mode is the escape hatch. No zoom/pan in v1 (YAGNI).
