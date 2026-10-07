"""Media helpers: PCM → WAV, frame + audio → mp4 (for the legacy Omni mode), tempo.

Uses the ffmpeg bundled with imageio-ffmpeg, so no system ffmpeg is needed.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import AsyncIterator

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 24000, channels: int = 1, width: int = 2) -> bytes:
    """Raw PCM16 → WAV container (in memory)."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def mux_clip(
    frame_jpeg: bytes,
    pcm: bytes,
    *,
    sample_rate: int = 24000,
    fps: float = 1.0,
    max_width: int = 640,
    min_duration_sec: float = 2.0,
) -> bytes:
    """One frame (JPEG) + utterance audio (PCM16 mono) → mp4 bytes.

    The frame is held as a still for the whole audio. Returns an mp4 ready to send
    as video_url (data URI).

    Token note: Omni samples frames from the video, so a long utterance = more still
    frames = more expensive. Minimized with a low fps (default 1) and downscaling to
    max_width (tokens per frame drop with resolution).
    """
    if not frame_jpeg:
        raise ValueError("mux_clip: empty frame")
    if not pcm:
        raise ValueError("mux_clip: empty audio")

    # the provider rejects too-short videos (400): pad the audio with silence
    min_bytes = int(min_duration_sec * sample_rate) * 2
    if len(pcm) < min_bytes:
        pcm = pcm + b"\x00" * (min_bytes - len(pcm))

    # scale to max_width (never upscale), even dimensions
    vf = f"scale='min({max_width},iw)':-2"

    with tempfile.TemporaryDirectory(prefix="smotri_mux_") as d:
        tmp = Path(d)
        img = tmp / "frame.jpg"
        wav = tmp / "audio.wav"
        out = tmp / "clip.mp4"
        img.write_bytes(frame_jpeg)
        wav.write_bytes(pcm_to_wav_bytes(pcm, sample_rate=sample_rate))

        cmd = [
            FFMPEG, "-y",
            "-loop", "1", "-framerate", str(fps), "-i", str(img),
            "-i", str(wav),
            "-vf", vf,
            "-r", str(fps),  # low output fps too → fewer frames in the container
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(out),
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed: {p.stderr[-500:]}")
        return out.read_bytes()


async def atempo_pcm_stream(
    pcm_chunks: AsyncIterator[bytes],
    *,
    tempo: float,
    sample_rate: int = 24000,
) -> AsyncIterator[bytes]:
    """Slow down/speed up a PCM16 stream without changing pitch (ffmpeg atempo).

    A persistent ffmpeg process: input chunks go to stdin, processed ones are read from
    stdout, keeping both streaming and continuity (no glitches at chunk boundaries).
    tempo<1 = slower/calmer, tempo>1 = faster. atempo allows 0.5..2.0; outside that,
    chain filters. tempo==1.0 → the caller should skip this.
    """
    if not (0.5 <= tempo <= 2.0):
        raise ValueError("atempo supports 0.5..2.0 in one filter")

    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-af", f"atempo={tempo}",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def feed() -> None:
        try:
            async for chunk in pcm_chunks:
                if chunk:
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
        finally:
            if proc.stdin:
                proc.stdin.close()

    feeder = asyncio.create_task(feed())
    try:
        while True:
            buf = await proc.stdout.read(8192)
            if not buf:
                break
            yield buf
    finally:
        feeder.cancel()
        try:
            await feeder
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
