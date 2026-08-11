// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG
// =============================================================================

/**
 * The `.rrapp` marker — the App Builder's per-working-copy identity file.
 *
 * `<folder>/<name>.rrapp` carries `{ id, projectId }`: `id` binds the folder
 * to its appManifest id, and `projectId` is a CLIENT-SIDE guid that tells
 * one working copy (checkout, duplicate) of the same app apart from another.
 * The projectId never becomes a server key — deploys record it only as
 * `metadata.projectId` provenance.
 *
 * One utility owns every read/write so the marker's shape cannot drift:
 * the App Builder editor (open), the scaffolder (create), and the publish
 * flow (provenance) all come through here.
 */

import * as vscode from 'vscode';
import { randomUUID } from 'crypto';

// =============================================================================
// TYPES
// =============================================================================

/** Parsed `.rrapp` marker content. */
export interface AppMarker {
	/** The appManifest id the folder is bound to (e.g. 'acme.brandy'). */
	id: string;
	/** Client-side working-copy guid (deploy provenance, never a server key). */
	projectId: string;
}

// =============================================================================
// MARKER ACCESS
// =============================================================================

/**
 * The marker URI for an app folder: `<folder>/<lastSegment(appId)>.rrapp`.
 *
 * @param folder - The app's bound folder (absolute path).
 * @param appId - The appManifest id.
 */
export function markerUriOf(folder: string, appId: string): vscode.Uri {
	return vscode.Uri.joinPath(vscode.Uri.file(folder), `${appId.split('.').pop()}.rrapp`);
}

/**
 * Reads, creates, or backfills the folder's `.rrapp` marker.
 *
 * - Missing marker: created with a fresh projectId (generate at creation).
 * - Marker without a projectId (pre-projectId working copies): backfilled
 *   in place with a fresh guid.
 * - Complete marker: returned as-is.
 *
 * @param folder - The app's bound folder (absolute path).
 * @param appId - The appManifest id the marker binds to.
 * @returns The marker content after any create/backfill.
 */
export async function ensureAppMarker(folder: string, appId: string): Promise<AppMarker> {
	const uri = markerUriOf(folder, appId);

	// Read whatever exists — malformed content is treated as absent
	let existing: Partial<AppMarker> = {};
	try {
		const raw = await vscode.workspace.fs.readFile(uri);
		const parsed = JSON.parse(Buffer.from(raw).toString('utf8')) as Partial<AppMarker>;
		if (parsed && typeof parsed === 'object') existing = parsed;
	} catch { /* absent or malformed — (re)create below */ }

	// Complete marker — nothing to do
	if (existing.id && existing.projectId) return existing as AppMarker;

	// Create or backfill: keep any existing id, generate the missing guid
	const marker: AppMarker = {
		id: existing.id || appId,
		projectId: existing.projectId || randomUUID(),
	};
	await vscode.workspace.fs.writeFile(uri, Buffer.from(`${JSON.stringify(marker, null, 2)}\n`, 'utf8'));
	return marker;
}
