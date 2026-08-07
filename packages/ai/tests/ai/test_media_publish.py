# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""MediaPublisher builds the right RTSP push per mime and resolves the WHEP url."""

from ai.account.media_publish import MediaPublisher, sfu_host


def test_video_copies_h264_and_targets_rtsp():
    pub = MediaPublisher('sfu.local', 'clip-abc', 'video/mp4')
    cmd = pub._cmd()
    assert '-c' in cmd and 'copy' in cmd  # H.264 copied, not re-encoded
    assert cmd[-1] == 'rtsp://sfu.local:8554/clip-abc'
    assert pub.whep_url == 'http://sfu.local:8889/clip-abc/whep'


def test_audio_transcodes_to_opus():
    cmd = MediaPublisher('h', 'a1', 'audio/mpeg')._cmd()
    assert 'libopus' in cmd  # WebRTC has no MP3
    assert cmd[-1] == 'rtsp://h:8554/a1'


def test_image_is_not_a_live_stream():
    assert MediaPublisher('h', 'i1', 'image/png')._cmd() is None


def test_sfu_host_reads_env(monkeypatch):
    monkeypatch.delenv('ROCKETRIDE_MEDIA_SFU', raising=False)
    assert sfu_host() is None
    monkeypatch.setenv('ROCKETRIDE_MEDIA_SFU', 'lab.local')
    assert sfu_host() == 'lab.local'


def test_feed_swallows_write_to_closed_stdin():
    # An encoder that died (RTSP refused) closes its stdin: feed must degrade, not raise.
    class _DeadStdin:
        closed = False

        def write(self, data):
            raise ValueError('write to closed file')

        def flush(self):
            pass

        def close(self):
            self.closed = True

    pub = MediaPublisher('sfu.local', 'clip', 'video/mp4')
    pub._proc = type('P', (), {'stdin': _DeadStdin()})()
    pub.feed(b'frame')  # must not raise
    assert pub.failed is True
    pub.feed(b'more')  # dead publisher is a silent no-op
