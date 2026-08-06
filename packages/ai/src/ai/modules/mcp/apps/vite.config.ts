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
		// es2022 (the repo-wide TS target) is modern enough to avoid that
		// downlevel path while staying inside the convention.
		target: 'es2022',
		rollupOptions: {
			// viteSingleFile inlines all JS into the HTML, so no JS chunk is
			// emitted — an entryFileNames option here would have no effect.
			input: resolve(__dirname, 'src', widget, 'index.html'),
		},
	},
});
