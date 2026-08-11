// MIT License
//
// Copyright (c) 2026 Aparavi Software AG
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

/**
 * App version overrides — the desktop version selector's session state.
 *
 * A user picking a specific app version (tile drop list, or a
 * `?appid=X&version=1.3.0` deep link) creates a SESSION-ONLY override:
 * stored in sessionStorage, dies with the tab, never persisted server-side
 * (persistent personal pins were rejected — they go stale silently).
 *
 * Resolution order (top wins): URL `?version=` → session override →
 * dev overlay → the server's scope-walk default in the manifest entry.
 * URL parsing simply seeds/replaces the session override at boot, so the
 * rest of the shell only ever consults ONE place.
 *
 * The override's minted entry URL is applied to the MF container either
 * synchronously at registration time (registerAndMapApps substitutes it —
 * the boot path, race-free) or via applyAppVersionOverride() when the user
 * picks a version mid-session (repoint when the container has not loaded
 * yet; reload otherwise — a loaded container can never be repointed, see
 * the MF one-document-one-version rule).
 */

import { repointRemote, isRemoteLoaded, invalidateAppDescriptor, isDevRemote } from './appLoader';

// =============================================================================
// STORAGE
// =============================================================================

/** sessionStorage key holding the per-app override map. */
const SS_OVERRIDES_KEY = 'rr:appVersionOverrides';

/**
 * One app's session version override.
 *
 * `version` starts as whatever the user expressed (registry version number
 * from the drop list, or a semver string from a deep link) and is
 * normalised to the registry version number once the server mints the
 * entry URL. `url` is absent until minted.
 */
export interface AppVersionOverride {
	/** Registry version number — THE wire version identity (ints only). */
	version: number;
	/** The resolved artifact semver — for chip display. */
	appVersion?: string;
	/** Signed entry URL minted by rrext_app_deploy/entry. */
	url?: string;
}

/**
 * Reads the full override map from sessionStorage.
 *
 * @returns App id → override; empty object when none or storage unavailable.
 */
export function getAppVersionOverrides(): Record<string, AppVersionOverride> {
	try {
		return JSON.parse(sessionStorage.getItem(SS_OVERRIDES_KEY) ?? '{}') as Record<string, AppVersionOverride>;
	} catch {
		return {};
	}
}

/**
 * Reads one app's session version override.
 *
 * @param appId - The app id.
 * @returns The override, or null when the app has none.
 */
export function getAppVersionOverride(appId: string): AppVersionOverride | null {
	return getAppVersionOverrides()[appId] ?? null;
}

/**
 * Writes (or replaces) one app's session version override.
 *
 * @param appId - The app id.
 * @param override - The override record to store.
 */
export function setAppVersionOverride(appId: string, override: AppVersionOverride): void {
	try {
		const map = getAppVersionOverrides();
		map[appId] = override;
		sessionStorage.setItem(SS_OVERRIDES_KEY, JSON.stringify(map));
	} catch { /* storage unavailable — override lasts this page only */ }
}

/**
 * Removes one app's session version override.
 *
 * @param appId - The app id.
 */
export function clearAppVersionOverride(appId: string): void {
	try {
		const map = getAppVersionOverrides();
		delete map[appId];
		sessionStorage.setItem(SS_OVERRIDES_KEY, JSON.stringify(map));
	} catch { /* storage unavailable */ }
}

// =============================================================================
// APPLICATION — repoint now, or reload when the container is committed
// =============================================================================

/**
 * Applies (or clears, with null) a version override to a live shell.
 *
 * The caller mints the entry URL first (client.appEntry) and passes the
 * full override; this function only handles the MF mechanics:
 *
 * - Container never loaded this session → force re-register at the new
 *   entry URL + evict the cached descriptor. The next launch loads the
 *   chosen version. Returns 'ready' — the caller may switch immediately.
 * - Container already loaded (or clearing an applied override) → a loaded
 *   MF container can never be repointed (identity is the NAME; forcing it
 *   corrupts the shared getters). Returns 'reload-required' — the caller
 *   reloads the page; boot then registers the override's URL (or the
 *   default, after a clear) before anything loads.
 *
 * Dev-owned containers are never touched — the live dev build always wins.
 *
 * @param appId - The app id.
 * @param moduleId - The app's MF container name.
 * @param override - The minted override, or null to reset to default.
 * @returns 'ready' when the switch can proceed in-place, else 'reload-required'.
 */
export function applyAppVersionOverride(
	appId: string,
	moduleId: string,
	override: AppVersionOverride | null,
): 'ready' | 'reload-required' {
	// The live dev build always wins — record/clear the override for later
	// sessions but never repoint a dev-owned container.
	if (isDevRemote(moduleId)) {
		if (override === null) clearAppVersionOverride(appId);
		else setAppVersionOverride(appId, override);
		return 'ready';
	}

	if (override === null) {
		// Reset to default resolution. If an override was ever applied this
		// session the container may sit at the overridden URL — only a fresh
		// boot deterministically restores the manifest default.
		const had = getAppVersionOverride(appId);
		clearAppVersionOverride(appId);
		return had?.url ? 'reload-required' : 'ready';
	}

	setAppVersionOverride(appId, override);
	// A loaded container is committed to its version for this document
	if (isRemoteLoaded(moduleId)) return 'reload-required';
	// Not loaded yet — repoint the registration and evict the descriptor
	if (override.url) {
		repointRemote(moduleId, override.url);
		invalidateAppDescriptor(appId);
	}
	return 'ready';
}
