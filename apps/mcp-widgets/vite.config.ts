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
		// @modelcontextprotocol/ext-apps bundles zod v4, whose source trips an
		// esbuild downlevel-destructuring bug against Vite's default baseline
		// target (chrome87/edge88/es2020/firefox78/safari14 — see esbuild#3488).
		// These widgets only ever run inside an MCP host's modern iframe
		// sandbox, never a general web page, so esnext is the right target.
		target: 'esnext',
		rollupOptions: {
			input: resolve(__dirname, 'src', widget, 'index.html'),
			output: { entryFileNames: `${widget}.js` },
		},
	},
});
