"""The mouth: core/viseme.py.

The transcript says what is being said; the audio says when. This module turns
the first into mouth shapes and fuses them onto the second's clock, and the two
failure modes it can have are both visible on screen:

  * shapes that do not match the sound (it looks like a different language),
  * a mouth that drifts ahead of the voice (it looks like a bad dub).

Everything here is pure arithmetic on lists, so it can be tested exactly — which
matters, because the alternative was watching a face and guessing.
"""
from __future__ import annotations

import pytest

from core.viseme import (VISEMES, VisemeStream, coverage, text_to_visemes,
                         to_latin)


class TestToLatin:
    """One reduction step replaces a per-language spelling table."""

    @pytest.mark.parametrize("char,expected", [
        ("a", "a"), ("Z", "z"),
        ("é", "e"), ("ü", "u"), ("ş", "s"), ("ğ", "g"), ("ñ", "n"), ("å", "a"),
        ("ế", "e"),                       # stacked Vietnamese marks
        ("α", "a"), ("ω", "o"),           # Greek
        ("д", "d"),                       # Cyrillic
    ])
    def test_it_reduces_to_a_latin_letter(self, char, expected):
        assert to_latin(char) == expected

    @pytest.mark.parametrize("char", ["1", "!", " ", "。", "€", "🤖", "\n"])
    def test_characters_with_no_sound_return_empty(self, char):
        assert to_latin(char) == ""

    def test_it_handles_undecomposed_forms(self):
        """NFD alone is not enough: ı/ß/æ are single codepoints with no
        decomposition, and they are common in the languages this is aimed at."""
        assert to_latin("ß") == "s"
        assert to_latin("ı") == "i"
        assert to_latin("æ") == "a"


class TestCoverage:
    def test_plain_english_is_fully_covered(self):
        assert coverage("hello there") == 1.0

    def test_it_ignores_non_letters(self):
        assert coverage("hi!! 123") == 1.0

    def test_text_with_no_letters_is_zero(self):
        assert coverage("123 !!") == 0.0
        assert coverage("") == 0.0

    def test_a_script_we_cannot_read_scores_low(self):
        # Cherokee syllabary: written form does not reveal pronunciation
        assert coverage("ᏣᎳᎩ") < 0.5


class TestTextToVisemes:
    def test_it_returns_shapes_for_english(self):
        out = text_to_visemes("hello")
        assert out and all(v in VISEMES for v, _ in out)

    def test_an_unreadable_script_returns_nothing_rather_than_guessing(self):
        """Empty tells the caller to fall back to the audio-only mouth. Making
        shapes up would be worse than not trying."""
        assert text_to_visemes("ᏣᎳᎩ ᏍᎩᏯ") == []

    def test_punctuation_becomes_a_rest(self):
        out = text_to_visemes("a, b")
        assert out[1][0] == "REST"

    def test_a_doubled_letter_is_one_sound(self):
        # avoiding digraphs on purpose: "oo" is a sound of its own, so "sob" vs
        # "soob" compares two different phonemes, not one letter doubled
        for single, doubled in (("so", "sso"), ("bet", "bett"), ("mad", "madd")):
            assert ([v for v, _ in text_to_visemes(single)]
                    == [v for v, _ in text_to_visemes(doubled)]), doubled

    def test_digraphs_beat_letter_by_letter_reading(self):
        """`sh` is one sound; reading it as s+h makes two shapes for one
        sound, which reads as a stutter."""
        shapes = [v for v, _ in text_to_visemes("shop")]
        assert shapes[0] == "S"

    def test_a_word_gap_does_not_close_the_mouth(self):
        """Closing between every word makes the avatar look like it is
        chewing."""
        out = text_to_visemes("go on")
        assert not any(v == "REST" for v, _ in out)

    def test_accents_do_not_change_the_shapes(self):
        assert text_to_visemes("cafe") == text_to_visemes("café")

    def test_case_does_not_change_the_shapes(self):
        assert text_to_visemes("Hello") == text_to_visemes("hello")

    def test_empty_text_is_empty(self):
        assert text_to_visemes("") == []


class TestVisemeStream:
    def test_it_queues_what_it_is_fed(self):
        s = VisemeStream()
        s.feed_text("hello")
        assert s.pending > 0

    def test_reset_empties_it(self):
        s = VisemeStream()
        s.feed_text("hello there")
        s.reset()
        assert s.pending == 0

    def test_a_backlog_is_capped(self):
        """A stalled turn must not pile up an unbounded queue — this runs for
        the whole life of a session."""
        s = VisemeStream()
        for _ in range(200):
            s.feed_text("the quick brown fox jumps over the lazy dog")
        assert s.pending <= 600

    def test_silence_does_not_consume_the_queue(self):
        """During a pause the mouth waits. Burning shapes on silence puts the
        mouth ahead of the voice, which is the drift this whole class exists to
        prevent."""
        s = VisemeStream()
        s.feed_text("hello")
        before = s.pending
        s.frames([(0.0, 0.0, 0.0)] * 40, hop=0.02)
        assert s.pending == before
        assert all(frame[1] == 0.0 for frame in s.frames([(0.0, 0, 0)], 0.02))

    def test_a_loud_frame_produces_a_shape(self):
        s = VisemeStream()
        s.feed_text("hello there")
        out = s.frames([(0.8, 0.1, 0.05)] * 5, hop=0.02)
        assert len(out) == 5
        assert all(0.0 <= o <= 1.0 for _, o, _ in out)

    def test_the_backlog_only_shrinks_while_audio_plays(self):
        s = VisemeStream()
        s.feed_text("hello there my friend")
        before = s.pending
        s.frames([(0.7, 0.2, 0.0)] * 10, hop=0.02)
        assert s.pending < before

    def test_a_long_backlog_speeds_the_clock_up(self):
        """Speech outrunning the clock must make the mouth catch up, not fall
        further behind."""
        idle, busy = VisemeStream(), VisemeStream()
        idle.feed_text("hi")
        for _ in range(50):
            busy.feed_text("the quick brown fox jumps over the lazy dog")
        assert busy._step_seconds() < idle._step_seconds()

    def test_the_step_stays_within_the_configured_bounds(self):
        s = VisemeStream()
        assert VisemeStream._MIN_STEP <= s._step_seconds() <= VisemeStream._MAX_STEP
        for _ in range(50):
            s.feed_text("a" * 200)
        assert s._step_seconds() >= VisemeStream._MIN_STEP

    def test_it_holds_the_last_shape_when_the_queue_empties(self):
        s = VisemeStream()
        s.feed_text("hi")
        s.frames([(0.9, 0.3, 0.0)] * 80, hop=0.05)      # way past the queue
        assert s.pending == 0
        out = s.frames([(0.9, 0.3, 0.0)], hop=0.05)
        assert out and 0.0 <= out[0][1] <= 1.0          # no crash, no NaN

    def test_outputs_are_clamped(self):
        """These numbers drive a painter; a value outside its range is a
        rendering artifact, so the clamp is part of the contract."""
        s = VisemeStream()
        s.feed_text("hello")
        out = s.frames([(1.0, 5.0, 9.0)] * 3, hop=0.02)
        for _, o, w in out:
            assert 0.0 <= o <= 1.0
            assert -1.0 <= w <= 1.0

    def test_every_viseme_has_a_definition(self):
        """A shape with no VISEMES entry would KeyError at paint time — on the
        playback coroutine, mid-sentence."""
        for text in ("ship", "chop", "think", "quick", "zoo", "yes", "mama",
                     "you", "wow", "eight", "phone", "knight", "box", ","):
            for shape, _ in text_to_visemes(text):
                assert shape in VISEMES, f"{shape!r} (from {text!r}) has no definition"

    def test_the_closures_are_between_zero_and_one(self):
        for name, (openness, wide, closure) in VISEMES.items():
            assert 0.0 <= openness <= 1.0, name
            assert -1.0 <= wide <= 1.0, name
            assert 0.0 <= closure <= 1.0, name
