/**
 * MIT License
 *
 * Copyright (c) 2026 Aparavi Software AG
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

/** A live WHEP stream: the media, and a closer that tears the connection down. */
export interface WhepStream {
	stream: MediaStream;
	close: () => void;
}

/** Resolve once ICE gathering finishes — WHEP is no-trickle (one offer, one answer). */
function iceComplete(pc: RTCPeerConnection): Promise<void> {
	if (pc.iceGatheringState === 'complete') return Promise.resolve();
	return new Promise(resolve => {
		pc.addEventListener('icegatheringstatechange', () => {
			if (pc.iceGatheringState === 'complete') resolve();
		});
	});
}

/**
 * Open a live WHEP egress stream: POST an SDP offer to `url`, play the answer's tracks.
 * The engine pushed this media to the SFU; the client pulls it here, never over the WS.
 */
export async function openWhepStream(url: string): Promise<WhepStream> {
	const pc = new RTCPeerConnection({ iceServers: [] });
	const stream = new MediaStream();
	// Egress only: server -> browser.
	pc.addTransceiver('video', { direction: 'recvonly' });
	pc.addTransceiver('audio', { direction: 'recvonly' });
	pc.ontrack = e => stream.addTrack(e.track);

	try {
		await pc.setLocalDescription(await pc.createOffer());
		await iceComplete(pc);
		const res = await fetch(url, {
			method: 'POST',
			headers: { 'Content-Type': 'application/sdp' },
			body: pc.localDescription?.sdp ?? '',
		});
		if (!res.ok) throw new Error(`WHEP ${res.status} ${res.statusText}`);
		await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() });
	} catch (err) {
		pc.close();
		throw err;
	}

	return { stream, close: () => pc.close() };
}
