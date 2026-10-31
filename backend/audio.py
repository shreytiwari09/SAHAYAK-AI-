"""Audio conversion with ffmpeg: WhatsApp voice notes are Ogg/Opus; Sarvam speaks and hears WAV."""

import asyncio
import shutil


class AudioError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _ffmpeg(args: list[str], data: bytes, timeout: float = 30.0) -> bytes:
    if not ffmpeg_available():
        raise AudioError("ffmpeg is not installed (brew install ffmpeg)")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", *args, "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout)
    except TimeoutError:
        proc.kill()
        raise AudioError("ffmpeg timed out") from None
    if proc.returncode != 0 or not out:
        raise AudioError(f"ffmpeg failed: {err.decode(errors='replace')[:200]}")
    return out


async def to_ogg_opus(audio: bytes) -> bytes:
    """Any audio to a mono Ogg/Opus voice note (what WhatsApp shows as a voice message)."""
    return await _ffmpeg(
        ["-vn", "-c:a", "libopus", "-b:a", "24k", "-ar", "16000", "-ac", "1"]
        + ["-application", "voip", "-f", "ogg"],
        audio,
    )  # fmt: skip


async def to_wav16k(audio: bytes) -> bytes:
    """Any audio to 16 kHz mono WAV, which speech-to-text handles most reliably."""
    return await _ffmpeg(["-vn", "-ar", "16000", "-ac", "1", "-f", "wav"], audio)
