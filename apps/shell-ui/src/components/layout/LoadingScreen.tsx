// MIT License — Copyright (c) 2026 Aparavi Software AG

// =============================================================================
// LOADING SCREEN — animated rocket mark shown during the boot/auth bootstrap
// =============================================================================
//
// Replaces the plain "Loading..." text with a mark gently bobbing up and down,
// mirroring home-ui's AuthTransitionPage so the shell's initial load and the
// post-OAuth transition read as one continuous animation. Theme-aware via the
// same CSS custom properties the rest of the shell uses.
//
// The mark is the RocketRide rocket while the SHELL boots, because at that
// point there is no app yet. Once an app is being loaded the caller passes that
// app's own icon: what is being waited for is the app, and showing the platform
// mark instead makes every app's load look identical to every other's.
// =============================================================================

import React, { useState, type CSSProperties } from 'react';
import { RocketRideMark } from 'shared';

const container: CSSProperties = {
	display: 'flex',
	height: '100vh',
	alignItems: 'center',
	justifyContent: 'center',
};

const logoWrapper: CSSProperties = {
	animation: 'rr-loading-float 2.4s ease-in-out infinite',
};

const KEYFRAMES = `@keyframes rr-loading-float {
	0%, 100% { transform: translateY(0); }
	50%       { transform: translateY(-8px); }
}`;

interface LoadingScreenProps {
	/**
	 * URL of the icon to bob, for when something specific is being loaded.
	 *
	 * Falls back to the RocketRide mark when absent or when the image fails —
	 * an app whose icon 404s should still get a loading screen, not a gap.
	 */
	iconUrl?: string;
	/** What the icon depicts, for screen readers. */
	iconAlt?: string;
}

/** Matches `RocketRideMark size={56}` so the bob does not change size. */
const ICON_SIZE = 56;

const LoadingScreen: React.FC<LoadingScreenProps> = ({ iconUrl, iconAlt = '' }) => {
	// Phase-anchor the float bob to a shared clock (epoch, realm-independent) so home-ui's
	// AuthTransitionPage picks up exactly where this leaves off — both use the same 2.4s
	// cycle. Computed once on mount so re-renders don't restart the animation.
	const [floatDelay] = useState(() => `${-(Date.now() % 2400)}ms`);
	// Reset when the URL changes: switching apps mid-load must not inherit the
	// previous app's failure.
	const [broken, setBroken] = useState(false);
	React.useEffect(() => setBroken(false), [iconUrl]);

	return (
		<div style={container}>
			<style>{KEYFRAMES}</style>
			<div style={{ ...logoWrapper, animationDelay: floatDelay }}>
				{iconUrl && !broken ? (
					<img
						src={iconUrl}
						alt={iconAlt}
						width={ICON_SIZE}
						height={ICON_SIZE}
						style={{ display: 'block', borderRadius: 10, objectFit: 'contain' }}
						onError={() => setBroken(true)}
					/>
				) : (
					<RocketRideMark size={ICON_SIZE} color="var(--rr-text-primary)" />
				)}
			</div>
		</div>
	);
};

export default LoadingScreen;
