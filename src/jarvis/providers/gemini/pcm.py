"""Gemini PCM boundaries; retain logical rates and never relabel samples."""

import re
from collections.abc import Iterator

from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.providers.gemini.errors import ProviderProtocolError

INPUT_PCM = PCMFormat()
OUTPUT_PCM = PCMFormat(sample_rate=24000)
MAX_AUDIO_CHUNK_BYTES = 96000
OUTPUT_MIME = re.compile(
    r"audio/pcm\s*;\s*rate=24000(?:\s*;\s*channels=1)?", re.IGNORECASE
)


def validate_audio_chunk(data: bytes, mime_type: str) -> None:
    if (
        not isinstance(data, bytes) or not 0 < len(data) <= MAX_AUDIO_CHUNK_BYTES
        or not isinstance(mime_type, str)
        or not OUTPUT_MIME.fullmatch(mime_type.strip())
    ):
        raise ProviderProtocolError(
            "Gemini output must be bounded raw 24 kHz mono S16_LE PCM."
        )


class OutputFramer:
    """Incremental 24 kHz framing with at most one partial 20 ms frame."""

    def __init__(self) -> None:
        self._partial = bytearray()
        self._sequence = 0
        self.padding_bytes = 0
        self.discarded_bytes = 0
        self.chunk_consumed_bytes = 0

    def feed(self, data: bytes, mime_type: str) -> Iterator[PCMFrame]:
        validate_audio_chunk(data, mime_type)
        offset = 0
        self.chunk_consumed_bytes = 0
        while offset < len(data):
            size = min(OUTPUT_PCM.frame_bytes - len(self._partial),
                       len(data) - offset)
            self._partial.extend(data[offset:offset + size])
            offset += size
            self.chunk_consumed_bytes = offset
            if len(self._partial) == OUTPUT_PCM.frame_bytes:
                payload = bytes(self._partial)
                self._partial.clear()
                frame = PCMFrame(OUTPUT_PCM, payload, self._sequence)
                self._sequence += 1
                yield frame

    def finish(self) -> PCMFrame | None:
        """Pad only the final aligned fragment with less than 20 ms silence."""
        if not self._partial:
            return None
        if len(self._partial) % OUTPUT_PCM.bytes_per_sample_frame:
            raise ProviderProtocolError("Gemini ended with a partial PCM sample.")
        padding = OUTPUT_PCM.frame_bytes - len(self._partial)
        self.padding_bytes += padding
        payload = bytes(self._partial) + bytes(padding)
        self._partial.clear()
        result = PCMFrame(OUTPUT_PCM, payload, self._sequence)
        self._sequence += 1
        return result

    def discard(self) -> int:
        """Invalidate interrupted partial audio without emitting padded voice."""
        size = len(self._partial)
        self.discarded_bytes += size
        self._partial.clear()
        return size
