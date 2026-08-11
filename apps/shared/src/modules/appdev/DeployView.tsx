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

// =============================================================================
// DEPLOY VIEW — versions rail + "Where this app is live"
// =============================================================================

/**
 * The DEPLOY view (the centerpiece): Publish snapshots an immutable version
 * (author, time, sha, commit-style message); Deploy pins a rung to one —
 * the single verb covering first publish, update, promote, and rollback
 * ("repoint, never rebuild").
 *
 * Layout per the v3 mockup: a horizontal rail of version cards headed by a
 * dashed "+ Publish" card, then the "Where this app is live" reverse index
 * (rung → pinned version → state → audience → time). Data arrives through
 * the host adapter; hosts that have not wired deploy yet get teaching empty
 * states instead of dead chrome.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { EmptyState } from 'shell';
import { StatusBadge } from 'shell';
import type { AppSummary, AppVersionInfo, IAppBuilderHost, RungKind, RungPin } from './types';

// =============================================================================
// TYPES
// =============================================================================

/** Props for the {@link DeployView} component. */
export interface IDeployViewProps {
	/** The host adapter (data + actions). */
	host: IAppBuilderHost;
	/** The app being shown (header facts). */
	app: AppSummary;
}

// =============================================================================
// STYLES
// =============================================================================

const styles: Record<string, React.CSSProperties> = {
	wrap: {
		overflow: 'auto',
		height: '100%',
	},
	head: {
		padding: '18px 26px 0',
	},
	h1: {
		fontSize: 20,
		fontWeight: 600,
		color: 'var(--rr-text-primary)',
	},
	sub: {
		fontSize: 12.5,
		color: 'var(--rr-text-secondary)',
		marginTop: 3,
		lineHeight: 1.5,
	},
	sectLabel: {
		padding: '18px 26px 8px',
		fontSize: 11,
		fontWeight: 700,
		letterSpacing: '0.06em',
		textTransform: 'uppercase',
		color: 'var(--rr-text-secondary)',
	},
	sectMicro: {
		fontWeight: 400,
		textTransform: 'none',
		letterSpacing: 0,
		color: 'var(--rr-text-disabled)',
		marginLeft: 8,
	},
	rail: {
		display: 'flex',
		gap: 12,
		padding: '0 26px',
		overflowX: 'auto',
		alignItems: 'stretch',
	},
	card: {
		minWidth: 225,
		maxWidth: 250,
		border: '1px solid var(--rr-border)',
		borderRadius: 8,
		background: 'var(--rr-bg-paper)',
		padding: '13px 15px',
		boxShadow: '0 1px 3px rgba(30,40,55,0.06)',
		flexShrink: 0,
	},
	cardVersion: {
		fontSize: 15,
		fontWeight: 700,
		color: 'var(--rr-text-primary)',
	},
	cardWho: {
		fontSize: 12,
		color: 'var(--rr-text-primary)',
		marginTop: 5,
	},
	cardWhen: {
		fontSize: 11,
		fontFamily: 'var(--rr-font-mono, Consolas, monospace)',
		color: 'var(--rr-text-secondary)',
		marginTop: 2,
	},
	cardMsg: {
		fontSize: 12,
		color: 'var(--rr-text-secondary)',
		fontStyle: 'italic',
		marginTop: 5,
	},
	chips: {
		display: 'flex',
		gap: 5,
		flexWrap: 'wrap',
		marginTop: 9,
		minHeight: 20,
	},
	cardAction: {
		marginTop: 10,
	},
	publishCard: {
		minWidth: 225,
		maxWidth: 250,
		border: '1.5px dashed var(--rr-border-hover)',
		borderRadius: 8,
		background: 'var(--rr-bg-paper)',
		padding: '13px 15px',
		display: 'flex',
		flexDirection: 'column',
		alignItems: 'center',
		justifyContent: 'center',
		textAlign: 'center',
		gap: 6,
		cursor: 'pointer',
		color: 'var(--rr-text-secondary)',
		flexShrink: 0,
	},
	publishPlus: {
		fontSize: 20,
		color: 'var(--rr-brand)',
	},
	publishTitle: {
		fontSize: 13,
		fontWeight: 700,
		color: 'var(--rr-text-primary)',
	},
	publishHint: {
		fontSize: 11,
		color: 'var(--rr-text-disabled)',
	},
	miniBtn: {
		padding: '4px 11px',
		fontSize: 11.5,
		background: 'var(--rr-bg-paper)',
		border: '1px solid var(--rr-border-hover)',
		borderRadius: 4,
		color: 'var(--rr-color-secondary)',
		fontWeight: 600,
		cursor: 'pointer',
		whiteSpace: 'nowrap',
	},
	livePanel: {
		margin: '20px 26px 30px',
		border: '1px solid var(--rr-border)',
		borderRadius: 8,
		overflow: 'hidden',
		background: 'var(--rr-bg-paper)',
	},
	liveHead: {
		padding: '11px 16px',
		fontSize: 13,
		fontWeight: 700,
		color: 'var(--rr-text-primary)',
		background: 'var(--rr-bg-surface-alt)',
		borderBottom: '1px solid var(--rr-border)',
	},
	liveRow: {
		display: 'flex',
		alignItems: 'center',
		gap: 10,
		padding: '11px 16px',
		borderTop: '1px solid var(--rr-bg-widget-header)',
		fontSize: 12.5,
	},
	liveRung: {
		width: 190,
		flexShrink: 0,
		color: 'var(--rr-text-primary)',
		fontWeight: 700,
	},
	liveHandle: {
		fontFamily: 'var(--rr-font-mono, Consolas, monospace)',
		fontSize: 11,
		color: 'var(--rr-text-secondary)',
		marginLeft: 6,
		fontWeight: 400,
	},
	pin: {
		fontSize: 11,
		fontWeight: 700,
		fontFamily: 'var(--rr-font-mono, Consolas, monospace)',
		padding: '2px 9px',
		borderRadius: 10,
		border: '1.5px solid var(--rr-color-secondary)',
		color: 'var(--rr-color-secondary)',
	},
	liveAudience: {
		flex: 1,
		color: 'var(--rr-text-secondary)',
		fontSize: 12,
	},
	liveWhen: {
		fontSize: 11.5,
		color: 'var(--rr-text-disabled)',
		whiteSpace: 'nowrap',
	},
	liveFoot: {
		padding: '10px 16px 12px',
		fontSize: 11.5,
		color: 'var(--rr-text-secondary)',
		lineHeight: 1.6,
		borderTop: '1px solid var(--rr-bg-widget-header)',
		background: 'var(--rr-bg-surface-alt)',
	},
	emptyWrap: {
		padding: '10px 26px 30px',
	},
	// "Publish to…" audience picker (Me / Team / Public), inline on a card.
	picker: {
		display: 'flex',
		gap: 6,
		flexWrap: 'wrap',
	},
	pickBtn: {
		padding: '4px 10px',
		fontSize: 11.5,
		fontWeight: 600,
		background: 'var(--rr-brand)',
		color: 'var(--rr-text-on-brand, #ffffff)',
		border: '1px solid transparent',
		borderRadius: 4,
		cursor: 'pointer',
		whiteSpace: 'nowrap',
	},
	pickBtnGhost: {
		padding: '4px 10px',
		fontSize: 11.5,
		fontWeight: 600,
		background: 'transparent',
		color: 'var(--rr-text-secondary)',
		border: '1px solid var(--rr-border)',
		borderRadius: 4,
		cursor: 'pointer',
	},
	// "Register as a developer" banner — shown until the org holds a developerId.
	devBanner: {
		margin: '14px 26px 0',
		padding: '12px 16px',
		border: '1px solid var(--rr-color-warning)',
		borderRadius: 8,
		background: 'rgba(232,185,49,0.10)',
	},
	devBannerText: {
		fontSize: 12.5,
		color: 'var(--rr-text-primary)',
		lineHeight: 1.5,
	},
	devBannerRow: {
		display: 'flex',
		gap: 8,
		marginTop: 10,
		alignItems: 'center',
	},
	devInput: {
		flex: '0 1 260px',
		background: 'var(--rr-bg-input)',
		border: '1px solid var(--rr-border)',
		borderRadius: 4,
		padding: '5px 10px',
		fontSize: 12.5,
		color: 'var(--rr-text-primary)',
		fontFamily: 'var(--rr-font-mono, Consolas, monospace)',
	},
	devError: {
		marginTop: 8,
		fontSize: 12,
		color: 'var(--rr-color-error)',
	},
};

// =============================================================================
// HELPERS
// =============================================================================

/** Rung chip label per rung kind (the mockup's uppercase chips). */
const RUNG_CHIP_LABEL: Record<RungKind, string> = {
	personal: 'ME',
	team: 'TEAM',
	public: 'STORE',
};

/** Per-version review-state badge (the deployment's review lifecycle). */
const STATE_BADGE: Record<NonNullable<AppVersionInfo['state']>, { variant: 'muted' | 'info' | 'success' | 'warning' | 'error'; label: string }> = {
	private: { variant: 'muted', label: 'draft' },
	submit: { variant: 'warning', label: 'in review' },
	ready: { variant: 'success', label: 'ready' },
	rejected: { variant: 'error', label: 'rejected' },
	failed: { variant: 'error', label: 'failed' },
};

/** Renders a unix-seconds timestamp as a compact local date/time. */
function formatWhen(unixSeconds?: number): string {
	if (!unixSeconds) return '';
	try {
		return new Date(unixSeconds * 1000).toLocaleString(undefined, {
			month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
		});
	} catch {
		return '';
	}
}

// =============================================================================
// COMPONENT
// =============================================================================

/**
 * Renders the DEPLOY view: version rail + where-live reverse index.
 *
 * @param props - See {@link IDeployViewProps}.
 */
export const DeployView: React.FC<IDeployViewProps> = ({ host, app }) => {
	// ── Data — loaded through the host adapter ───────────────────────────
	const [versions, setVersions] = useState<AppVersionInfo[] | null>(null);
	const [pins, setPins] = useState<RungPin[] | null>(null);
	// Which version's "Publish to…" audience picker is open (null = none).
	const [pickerFor, setPickerFor] = useState<string | null>(null);
	// Developer registration: '' = org not a developer yet; null = unknown/loading.
	const [developerId, setDeveloperId] = useState<string | null>(null);
	const [regSlug, setRegSlug] = useState('');
	const [regBusy, setRegBusy] = useState(false);
	const [regError, setRegError] = useState('');

	/** Loads (or reloads) versions + pins; absent loaders resolve empty. */
	const refresh = useCallback(async (): Promise<void> => {
		try {
			const [v, p] = await Promise.all([
				host.listVersions?.() ?? Promise.resolve([]),
				host.getWhereLive?.() ?? Promise.resolve([]),
			]);
			setVersions(v);
			setPins(p);
		} catch (e) {
			console.log('[appdev] deploy refresh failed:', e);
			setVersions([]);
			setPins([]);
		}
	}, [host]);

	useEffect(() => { void refresh(); }, [refresh]);

	// Load the org's developer id (null when the host can't report it — no banner).
	useEffect(() => {
		if (!host.getDeveloperId) { setDeveloperId(null); return; }
		void host.getDeveloperId().then(setDeveloperId).catch(() => setDeveloperId(null));
	}, [host]);

	/** Claim the org's developer id slug (org.admin, self-service). */
	const onRegister = useCallback(async (): Promise<void> => {
		if (!host.registerDeveloper || !regSlug.trim()) return;
		setRegBusy(true);
		setRegError('');
		try {
			const assigned = await host.registerDeveloper(regSlug.trim());
			setDeveloperId(assigned);
			setRegSlug('');
		} catch (e) {
			setRegError(e instanceof Error ? e.message : String(e));
		} finally {
			setRegBusy(false);
		}
	}, [host, regSlug]);

	/** Deploy: snapshot the current build as the next immutable registry
	 * version ("Deploy" = copy code to the server). Binds nothing yet — that
	 * is the separate publish step. (Rides the host's build action.) */
	const onDeployBuild = useCallback(async (): Promise<void> => {
		if (!host.deploy) return;
		const message = window.prompt('Deploy message (what changed):') ?? '';
		if (!message) return;
		await host.deploy(message);
		await refresh();
	}, [host, refresh]);

	/** Publish: bind a version to an audience — @me, a team, or the public
	 * store. The one verb for first publish, update, promote, and rollback.
	 * @public needs the version approved (ready) first. */
	const onPublishTo = useCallback(async (version: string, choice: 'me' | 'team' | 'public'): Promise<void> => {
		if (!host.publish) return;
		let target: string;
		if (choice === 'me') target = '@me';
		else if (choice === 'public') target = '@public';
		else {
			const name = window.prompt('Team name to publish to (@team/<name>):') ?? '';
			if (!name.trim()) { setPickerFor(null); return; }
			target = `@team/${name.trim()}`;
		}
		setPickerFor(null);
		await host.publish(version, target);
		await refresh();
	}, [host, refresh]);

	/** Submit a deployed version for public store review (private → submit). */
	const onSubmit = useCallback(async (version: string): Promise<void> => {
		if (!host.submitForReview) return;
		await host.submitForReview(version);
		await refresh();
	}, [host, refresh]);

	const deployWired = Boolean(host.listVersions || host.deploy);

	return (
		<div style={styles.wrap}>
			{/* View header — title + one-line purpose (the pipeline pattern) */}
			<div style={styles.head}>
				<div style={styles.h1}>{app.name}</div>
				<div style={styles.sub}>
					Deploy immutable versions, then publish each to an audience — @me, a team, or the public store.
					Internal audiences serve instantly; the store gates every version on review.
				</div>
			</div>

			{!deployWired ? (
				<div style={styles.emptyWrap}>
					<EmptyState
						title="Publishing is not wired up yet"
						description="Once the deploy pipeline lands, published versions appear here as immutable cards with rung chips, and the reverse index below shows what runs where."
					/>
				</div>
			) : (
				<>
					{host.registerDeveloper && developerId === '' && (
						<div style={styles.devBanner}>
							<div style={styles.devBannerText}>
								<strong>Register as a developer to deploy apps.</strong> Every app id is <code>&lt;developerId&gt;.&lt;name&gt;</code> — claim your organization&rsquo;s developer id (letters and underscores only) to publish under your own namespace.
							</div>
							<div style={styles.devBannerRow}>
								<input
									style={styles.devInput}
									placeholder="developer id (e.g. acme_labs)"
									value={regSlug}
									onChange={(e) => setRegSlug(e.target.value)}
								/>
								<button style={styles.pickBtn} disabled={regBusy || !regSlug.trim()} onClick={() => void onRegister()}>
									{regBusy ? 'Registering…' : 'Register'}
								</button>
							</div>
							{regError ? <div style={styles.devError}>{regError}</div> : null}
						</div>
					)}
					{/* Published versions rail */}
					<div style={styles.sectLabel}>
						Published versions
						<span style={styles.sectMicro}>org registry · immutable · newest first</span>
					</div>
					<div style={styles.rail}>
						{host.deploy && (
							<div style={styles.publishCard} onClick={() => void onDeployBuild()}>
								<span style={styles.publishPlus}>+</span>
								<span style={styles.publishTitle}>Deploy</span>
								<span style={styles.publishHint}>snapshot the current build to the server</span>
							</div>
						)}
						{(versions ?? []).map((v) => (
							<div key={v.version} style={styles.card}>
								<div style={styles.cardVersion}>v{v.version}</div>
								<div style={styles.cardWho}>{v.author}</div>
								<div style={styles.cardWhen}>
									{formatWhen(v.publishedAt)}{v.sha ? ` · ${v.sha.slice(0, 8)}…` : ''}
								</div>
								{v.message ? <div style={styles.cardMsg}>&ldquo;{v.message}&rdquo;</div> : null}
								<div style={styles.chips}>
									{v.state ? (
										<StatusBadge variant={STATE_BADGE[v.state].variant}>{STATE_BADGE[v.state].label}</StatusBadge>
									) : null}
									{v.rungs.map((r) => (
										<StatusBadge key={r} variant={r === 'public' ? 'info' : 'success'}>
											{RUNG_CHIP_LABEL[r]}
										</StatusBadge>
									))}
								</div>
								{(host.publish || host.submitForReview) && (
									<div style={styles.cardAction}>
										{host.submitForReview && (
											<button style={styles.miniBtn} onClick={() => void onSubmit(v.version)}>Submit for review</button>
										)}
										{host.publish && (pickerFor === v.version ? (
											<div style={styles.picker}>
												<button style={styles.pickBtn} onClick={() => void onPublishTo(v.version, 'me')}>Me</button>
												<button style={styles.pickBtn} onClick={() => void onPublishTo(v.version, 'team')}>Team…</button>
												<button style={styles.pickBtn} onClick={() => void onPublishTo(v.version, 'public')}>Public</button>
												<button style={styles.pickBtnGhost} onClick={() => setPickerFor(null)}>Cancel</button>
											</div>
										) : (
											<button style={styles.miniBtn} onClick={() => setPickerFor(v.version)}>Publish to…</button>
										))}
									</div>
								)}
							</div>
						))}
					</div>

					{/* Where this app is live — the reverse index */}
					<div style={styles.livePanel}>
						<div style={styles.liveHead}>Where this app is live</div>
						{(pins ?? []).length === 0 ? (
							<div style={styles.liveFoot}>
								Nothing is deployed yet — publish a version and deploy it to your personal rung to see it here.
							</div>
						) : (
							<>
								{(pins ?? []).map((p) => (
									<div key={p.rung + p.handle} style={styles.liveRow}>
										<div style={styles.liveRung}>
											{p.label}
											<span style={styles.liveHandle}>{p.handle}</span>
										</div>
										<span style={styles.pin}>v{p.version}</span>
										<StatusBadge variant={p.state === 'pending' ? 'warning' : 'success'}>
											{p.state === 'pending' ? 'in review' : p.state}
										</StatusBadge>
										<span style={styles.liveAudience}>
											{p.audience}
											{p.pendingVersion ? ` · v${p.pendingVersion} in review` : ''}
										</span>
										<span style={styles.liveWhen}>
											{p.deployedAt ? `deployed ${formatWhen(p.deployedAt)}` : ''}
										</span>
									</div>
								))}
								<div style={styles.liveFoot}>
									Deploy pins a rung to an immutable version — first publish, update, promote, and rollback
									are all this one verb. Personal deploys land on your desktop automatically. Review gates
									every version on the store rung; internal rungs never wait.
								</div>
							</>
						)}
					</div>
				</>
			)}
		</div>
	);
};
