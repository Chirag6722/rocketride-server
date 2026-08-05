/**
 * MIT License
 * Copyright (c) 2026 Aparavi Software AG
 * See LICENSE file for details.
 */

import React, { useEffect, useState } from 'react';
import { getClient } from '../hooks/clientSingleton';

interface InlineMediaRendererProps {
	/** FileStore path; the bytes are pulled over rrext_media. */
	path?: string | undefined;
	/** Pre-resolved data URI (base64 fallback — nothing to pull). */
	directUrl?: string | undefined;
	mime?: string | undefined;
	name?: string | undefined;
}

type Source = { kind: 'loading' } | { kind: 'url'; url: string } | { kind: 'error'; message: string };

const categoryOf = (mime?: string): 'audio' | 'video' | 'image' | undefined => {
	if (mime?.startsWith('audio/')) return 'audio';
	if (mime?.startsWith('video/')) return 'video';
	if (mime?.startsWith('image/')) return 'image';
	return undefined;
};

/** Plays media produced by a pipeline, streaming it while the node still writes. */
export const InlineMediaRenderer: React.FC<InlineMediaRendererProps> = ({ path, directUrl, mime, name }) => {
	const label = name ?? path?.split('/').pop() ?? 'media';
	const category = categoryOf(mime);
	const [source, setSource] = useState<Source>(directUrl ? { kind: 'url', url: directUrl } : { kind: 'loading' });

	useEffect(() => {
		if (directUrl || !path) return;
		const client = getClient();
		if (!client) {
			setSource({ kind: 'error', message: 'Not connected — cannot load media.' });
			return;
		}

		let cancelled = false;
		let objectUrl: string | undefined;

		client
			.mediaPlaybackUrl(path, mime)
			.then(url => {
				objectUrl = url;
				if (!cancelled) setSource({ kind: 'url', url });
			})
			.catch((e: unknown) => {
				if (!cancelled) setSource({ kind: 'error', message: e instanceof Error ? e.message : 'Failed to load media.' });
			});

		return () => {
			cancelled = true;
			if (objectUrl) URL.revokeObjectURL(objectUrl);
		};
	}, [path, directUrl, mime]);

	if (source.kind === 'loading') {
		return (
			<div className="inline-media inline-media-loading">
				<span className="inline-media-name">{label}</span>
				<span className="inline-media-spinner" />
			</div>
		);
	}

	if (source.kind === 'error') {
		return (
			<div className="inline-media inline-media-error">
				<span className="inline-media-name">{label}</span>
				<span className="inline-media-errortext">{source.message}</span>
			</div>
		);
	}

	return (
		<div className={`inline-media inline-media-${category ?? 'file'}`}>
			{category === 'video' && <video className="inline-media-video" src={source.url} controls preload="metadata" />}
			{category === 'audio' && <audio className="inline-media-audio" src={source.url} controls preload="metadata" />}
			{category === 'image' && <img className="inline-media-image" src={source.url} alt={label} />}
			<div className="inline-media-footer">
				<span className="inline-media-name">{label}</span>
			</div>
		</div>
	);
};
