# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Push a produced media lane to an SFU so the client pulls it live over WHEP.

The bundled ffmpeg has no WHIP muxer but does have RTSP; MediaMTX ingests the RTSP
and re-exposes it as WHEP — one ffmpeg per stream, bytes in on stdin, RTSP out.
"""

import os
import subprocess
import threading

# Host of the SFU (MediaMTX). Unset => no live media-plane; caller falls back.
_SFU_ENV = 'ROCKETRIDE_MEDIA_SFU'
_RTSP_PORT = 8554
_WHEP_PORT = 8889
_STDERR_TAIL = 4096
_FINISH_TIMEOUT = 30


def sfu_host():
    """The configured SFU host, or None when the live media-plane is off."""
    return os.environ.get(_SFU_ENV) or None


def _ffmpeg_exe():
    """The bundled ffmpeg (same one video_composer uses), or PATH's as a fallback."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, AttributeError):
        return 'ffmpeg'


class MediaPublisher:
    """One ffmpeg pushing a lane's bytes to the SFU over RTSP; WHEP url for the client."""

    def __init__(self, host: str, stream_id: str, mime: str):
        self._host = host
        self._id = stream_id
        self._mime = mime or ''
        self._proc = None
        self._stderr_tail = b''
        self._dead = False

    @property
    def whep_url(self) -> str:
        """Where the client pulls this stream (MediaMTX re-exposes the RTSP as WHEP)."""
        return f'http://{self._host}:{_WHEP_PORT}/{self._id}/whep'

    @property
    def failed(self) -> bool:
        """True once the encoder died mid-stream; diagnose the cause via stderr_tail."""
        return self._dead

    @property
    def stderr_tail(self) -> str:
        """The tail of ffmpeg's diagnostics (e.g. 'Connection refused'), decoded lossily."""
        return self._stderr_tail.decode('utf-8', 'replace').strip()

    def _cmd(self):
        """Ffmpeg argv: copy H.264 video as-is; transcode audio to Opus (WebRTC needs it)."""
        if self._mime.startswith('video/'):
            codec = ['-c', 'copy']
        elif self._mime.startswith('audio/'):
            codec = ['-c:a', 'libopus', '-b:a', '128k']
        else:
            return None  # a single image is not a live stream
        url = f'rtsp://{self._host}:{_RTSP_PORT}/{self._id}'
        return [
            _ffmpeg_exe(),
            '-hide_banner',
            '-loglevel',
            'error',
            # Start on the first bytes instead of probing a long header.
            '-probesize',
            '32',
            '-analyzeduration',
            '0',
            '-fflags',
            '+nobuffer+genpts',
            '-i',
            'pipe:0',
            *codec,
            '-f',
            'rtsp',
            '-rtsp_transport',
            'tcp',
            url,
        ]

    def begin(self) -> bool:
        """Launch the RTSP push. Returns False if the mime isn't streamable or ffmpeg won't start."""
        cmd = self._cmd()
        if cmd is None:
            return False
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except (OSError, subprocess.SubprocessError):
            self._proc = None
            return False
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return True

    def feed(self, data: bytes) -> None:
        """Push one chunk. Once the encoder dies it becomes a no-op, never raising."""
        if self._proc is None or self._dead or not data:
            return
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            # Encoder died (RTSP refused, bad codec): stop feeding, media falls back to the spool.
            self._dead = True
            self._close_stdin()

    def finish(self) -> None:
        """Close the input and reap ffmpeg; kill it if it overruns the timeout."""
        self._close_stdin()
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=_FINISH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def _close_stdin(self) -> None:
        if self._proc is not None and self._proc.stdin and not self._proc.stdin.closed:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def _drain_stderr(self) -> None:
        """Keep the tail of ffmpeg's diagnostics and stop its stderr pipe from filling."""
        try:
            self._stderr_tail = self._proc.stderr.read()[-_STDERR_TAIL:]
        except (OSError, ValueError):
            pass
