"""Provider PCM framing is incremental, bounded, and independent of hardware."""

import unittest

from jarvis.providers.gemini.errors import ProviderProtocolError
from jarvis.providers.gemini.pcm import (
    OUTPUT_PCM, OutputFramer, validate_audio_chunk,
)

MIME = "audio/pcm;rate=24000"


class GeminiPcmTests(unittest.TestCase):
    def test_output_rate_and_bytes_are_not_relabelled(self):
        self.assertEqual(OUTPUT_PCM.sample_rate, 24000)
        self.assertEqual(OUTPUT_PCM.samples_per_frame, 480)
        self.assertEqual(OUTPUT_PCM.frame_bytes, 960)
        framer = OutputFramer()
        frames = list(framer.feed(bytes(1920), MIME))
        self.assertEqual([frame.sequence for frame in frames], [0, 1])
        self.assertEqual([frame.format for frame in frames], [OUTPUT_PCM] * 2)

    def test_partial_and_sample_split_chunks_preserve_all_bytes(self):
        payload = bytes(range(256)) * 15
        framer = OutputFramer()
        frames = []
        for start, end in ((0, 17), (17, 701), (701, 1023), (1023, 3840)):
            frames.extend(framer.feed(payload[start:end], MIME))
            self.assertLess(len(framer._partial), 960)
        self.assertEqual(b"".join(frame.payload for frame in frames), payload)
        self.assertIsNone(framer.finish())

    def test_final_sample_aligned_fragment_is_padded_and_accounted(self):
        framer = OutputFramer()
        self.assertEqual(list(framer.feed(b"x" * 100, MIME)), [])
        frame = framer.finish()
        self.assertEqual(frame.payload, b"x" * 100 + bytes(860))
        self.assertEqual(framer.padding_bytes, 860)
        self.assertIsNone(framer.finish())

    def test_incomplete_final_sample_is_rejected(self):
        framer = OutputFramer()
        list(framer.feed(b"x", MIME))
        with self.assertRaises(ProviderProtocolError):
            framer.finish()

    def test_mismatched_or_unbounded_provider_audio_is_rejected(self):
        for data, mime in (
            (bytes(960), "audio/pcm;rate=16000"),
            (bytes(960), "audio/wav"), (bytes(960), "audio/pcm"),
            (bytes(960), "audio/pcm;rate=24000;channels=2"),
            (bytes(96001), MIME), (b"", MIME), ("secret-value", MIME),
        ):
            with self.subTest(mime=mime):
                with self.assertRaises(ProviderProtocolError) as raised:
                    validate_audio_chunk(data, mime)
                self.assertNotIn("secret-value", str(raised.exception))

    def test_interruption_discards_partial_audio_and_preserves_order(self):
        framer = OutputFramer()
        first = list(framer.feed(bytes(960), MIME))[0]
        list(framer.feed(b"stale" * 10, MIME))
        self.assertEqual(framer.discard(), 50)
        new = list(framer.feed(b"n" * 960, MIME))[0]
        self.assertGreater(new.sequence, first.sequence)
        self.assertEqual(new.payload, b"n" * 960)
        self.assertIsNone(framer.finish())
