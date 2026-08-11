// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG
// =============================================================================

/**
 * Deploy flow — copy an immutable app version to the server from VSCode.
 *
 * Deploys ALWAYS use the real rsbuild build (decision D5 — browser-linked
 * dev output is never uploaded): a one-shot `rsbuild build` in the app
 * folder, then ONE zip (dist/* at the zip root + package.json carrying the
 * full appManifest) rides the generic `rrext_deploy add` rail door. The
 * server retains the zip and unpacks it at receipt; deploying never
 * activates anything — the Deploy view publishes rungs.
 */

import * as vscode from 'vscode';
import * as path from 'path';
import { promises as fs } from 'fs';
import { spawn } from 'child_process';
import AdmZip from 'adm-zip';
import { ConnectionManager } from '../connection/connection';
import { scanWorkspaceApps } from './appScan';
import { ensureAppMarker } from './appMarker';
import { resolveRsbuildInvocation } from './watchManager';
import { getLogger } from '../shared/util/output';

// =============================================================================
// CONSTANTS
// =============================================================================

/** Upper bound for the one-shot publish build (template-scale apps build in
 * seconds — ten minutes only ever means a wedged process). */
const BUILD_TIMEOUT_MS = 10 * 60 * 1000;

// =============================================================================
// PUBLISH
// =============================================================================

/**
 * Builds the app and publishes the bundle as an immutable version.
 *
 * @param appId - The app to publish (appManifest.id).
 * @param message - Commit-style "what changed" note for the version card.
 * @returns The new version-rail entry.
 */
export async function deployApp(appId: string, message: string): Promise<Record<string, unknown>> {
	const logger = getLogger();
	const apps = await scanWorkspaceApps();
	const app = apps.find((a) => a.id === appId);
	if (!app) throw new Error(`App "${appId}" has no bound folder in this workspace.`);

	const client = ConnectionManager.getInstance().getClient();
	if (!client || !ConnectionManager.getInstance().isConnected()) {
		throw new Error('Not connected — publishing needs a live server connection.');
	}

	// ── The canonical build (decision D5) ────────────────────────────────
	logger.output(`[appdev] publish build: ${appId}`);
	const invocation = resolveRsbuildInvocation(app.folder);
	await new Promise<void>((resolve, reject) => {
		const proc = spawn(invocation.cmd, [...invocation.args, 'build'], {
			cwd: app.folder,
			shell: invocation.shell,
			env: { ...process.env, NO_COLOR: '1' },
		});
		let tail = '';
		proc.stdout?.on('data', (c: Buffer) => { tail = (tail + c.toString('utf8')).slice(-2000); });
		proc.stderr?.on('data', (c: Buffer) => { tail = (tail + c.toString('utf8')).slice(-2000); });
		// Settle exactly once — close, spawn-error, and the timeout race here.
		let settled = false;
		const finish = (err?: Error): void => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			if (err) reject(err); else resolve();
		};
		// A hung build would otherwise leave the panel's publish RPC pending
		// forever.
		const timer = setTimeout(() => {
			try { proc.kill('SIGKILL'); } catch { /* already gone */ }
			finish(new Error(`rsbuild build timed out after ${BUILD_TIMEOUT_MS / 60000} minutes: ${tail.slice(-400)}`));
		}, BUILD_TIMEOUT_MS);
		// 'close' (not 'exit') so the stdio tail is complete when a failure
		// names its cause.
		proc.on('close', (code) => (code === 0 ? finish() : finish(new Error(`rsbuild build failed (${code}): ${tail.slice(-400)}`))));
		proc.on('error', (err) => finish(err));
	});

	// ── Pack the built bundle: dist/* at the zip root + package.json ─────
	// The packed package.json carries the FULL appManifest — the server
	// reads it as metadata.manifest, the listing truth for this version.
	const distDir = path.join(app.folder, 'dist');
	const zip = new AdmZip();
	zip.addLocalFolder(distDir);
	zip.addLocalFile(path.join(app.folder, 'package.json'));
	const data = new Uint8Array(zip.toBuffer());

	// ── Working-copy provenance: the client-side .rrapp projectId ────────
	const marker = await ensureAppMarker(app.folder, appId);

	// ── The ONE rail door (deploy = copy code to the server) ─────────────
	const body = await client.deploy.add({
		kind: 'app',
		data,
		metadata: { projectId: marker.projectId },
		comment: message,
	});
	const entry = (body as Record<string, unknown>)?.artifact as Record<string, unknown> | undefined;
	if (!entry) throw new Error(`Deploy returned no artifact entry for ${appId}.`);
	// The registry's answer is the truth about what was deployed — report
	// the SAME version in the log and the toast (the manifest's app.version
	// is only the fallback).
	const deployedVersion = (entry.appVersion as string) ?? app.version;
	logger.output(`[appdev] deployed ${appId} v${deployedVersion} (registry v${entry.registryVersion})`);
	vscode.window.showInformationMessage(`Deployed ${app.name} v${deployedVersion} — publish it from the Deploy view to make it live.`);
	return entry;
}
