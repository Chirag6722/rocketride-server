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

import { mountBrandHeader } from '../shared/brand';
import '../shared/theme.css';

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
	table.innerHTML = '<thead><tr><th>Name</th><th>Description</th><th>Token</th><th></th></tr></thead>';
	const tbody = document.createElement('tbody');
	for (const row of rows) {
		const tr = document.createElement('tr');
		const cells = [row.name, row.description ?? '', row.token].map((v, i) => {
			const td = document.createElement('td');
			if (i === 2) td.className = 'rr-mono';
			td.textContent = v;
			return td;
		});
		const actions = document.createElement('td');
		const stop = document.createElement('button');
		stop.className = 'rr-btn rr-btn-danger';
		stop.textContent = 'Terminate';
		stop.onclick = async () => {
			stop.disabled = true;
			try {
				// terminate's schema requires task_token (see execution.py _TERMINATE_SCHEMA).
				await app.callServerTool({ name: 'terminate', arguments: { task_token: row.token } });
				await refresh();
			} catch (err) {
				stop.disabled = false;
				stop.textContent = 'Terminate (failed — retry)';
				console.error('terminate failed', err);
			}
		};
		actions.appendChild(stop);
		tr.append(...cells, actions);
		tbody.appendChild(tr);
	}
	table.appendChild(tbody);
	const card = document.createElement('div');
	card.className = 'rr-card';
	card.appendChild(table);
	root.replaceChildren(card);

	const reload = document.createElement('button');
	reload.className = 'rr-btn rr-btn-ghost';
	reload.textContent = 'Refresh';
	reload.onclick = async () => {
		try {
			await refresh();
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			root.textContent = `Refresh failed: ${msg}`;
			console.error('refresh failed', err);
		}
	};
	root.appendChild(reload);
}

async function refresh(): Promise<void> {
	try {
		const result = await app.callServerTool({ name: 'list_running_pipelines', arguments: {} });
		render(parseRows(result));
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		root.textContent = `Failed to refresh pipelines: ${msg}`;
		console.error('refresh failed', err);
	}
}

mountBrandHeader('RocketRide Pipelines');

// Initial data: the host pushes the tool result that triggered this widget.
app.ontoolresult = (result) => {
	try {
		render(parseRows(result));
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		root.textContent = `Failed to render pipelines: ${msg}`;
		console.error('render failed', err);
	}
};
app.connect();
