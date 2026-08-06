/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 */

/**
 * MCP Widgets Build Module
 *
 * Single-file HTML widgets (MCP Apps) served by the ai MCP module. The
 * workspace lives inside the module (packages/ai/src/ai/modules/mcp/apps),
 * so vite builds straight into the served dist/ — no copy step. ai:sync
 * excludes everything here except dist/, keeping the toolchain out of the
 * server dist.
 */
const path = require('path');
const { execCommand, removeDir, hasBuildInputChanged, saveSourceHash, setState, exists, move } = require('../../../../../../../../scripts/lib');

// Paths
const APP_ROOT = path.join(__dirname, '..');
const DIST_DIR = path.join(APP_ROOT, 'dist');

// Build inputs: own src + package.json
const SRC_DIR = path.join(APP_ROOT, 'src');
const PKG_JSON = path.join(APP_ROOT, 'package.json');
const BUILD_HASH_KEY = 'mcp-widgets.buildHash';

const WIDGETS = ['pipelines-table', 'dropper'];

// =============================================================================
// ACTION FACTORIES
// =============================================================================

/**
 * Bundles each widget via vite with build-input caching. Vite's outDir is the
 * served dist/ next to this workspace, which is where apps.py reads from.
 */
function makeBundleAction() {
	return {
		run: async (ctx, task) => {
			// Fingerprint inputs before building so concurrent edits are detected on the next run.
			const { changed, hash } = await hasBuildInputChanged(BUILD_HASH_KEY, [SRC_DIR], [PKG_JSON]);
			if (!ctx.options.force && !changed && (await exists(DIST_DIR))) {
				task.output = 'No changes detected';
				return;
			}

			// Clean build output before rebuilding to prevent stale chunks
			await removeDir(DIST_DIR);
			for (const widget of WIDGETS) {
				task.output = `Building ${widget}...`;
				await execCommand('pnpm', ['run', 'build'], {
					task,
					cwd: APP_ROOT,
					env: { ...process.env, WIDGET: widget },
				});
				// Vite emits the entry HTML under its source filename (index.html),
				// not entryFileNames (that option only renames JS chunks). Rename
				// to the widget-specific filename apps.py expects, so a second
				// widget in this loop doesn't overwrite the first.
				await move(path.join(DIST_DIR, 'index.html'), path.join(DIST_DIR, `${widget}.html`));
			}

			await saveSourceHash(BUILD_HASH_KEY, hash);
		},
	};
}

function makeCleanAction() {
	return {
		run: async (ctx, task) => {
			await removeDir(DIST_DIR);
			await setState(BUILD_HASH_KEY, null);
			task.output = 'Cleaned mcp-widgets';
		},
	};
}

// =============================================================================
// MODULE DEFINITION
// =============================================================================

module.exports = {
	name: 'mcp-widgets',
	description: 'MCP Apps Widgets (embedded UI served by the ai MCP module)',

	actions: [
		{
			name: 'mcp-widgets:build',
			action: () => ({
				description: 'Build MCP Apps widgets',
				...makeBundleAction(),
			}),
		},
		{
			name: 'mcp-widgets:clean',
			action: () => ({
				description: 'Cleaning mcp-widgets',
				...makeCleanAction(),
			}),
		},
	],
};
