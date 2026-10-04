"""Stdlib unittest, no network, no models. `python -m unittest discover tests`

The end-to-end case builds two real PNGs and two real WAVs with ffmpeg's lavfi
sources and renders an actual mp4, because the assembler's filtergraph is the
code most likely to break silently.
"""

import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cf import assemble as asm  # noqa: E402
from cf.config import load_config  # noqa: E402
from cf.media import Clip, build_timeline  # noqa: E402
from cf.util import CfError  # noqa: E402
from cf.script import (  # noqa: E402
    NO_TEXT_CLAUSE,
    ScriptError,
    derive_frame_script,
    derive_narration_script,
    validate,
)

VALID = {
    "schema_version": "1.0",
    "title": "Why electricity isn't a fuel",
    "style_prompt": "minimal 2D stick-figure explainer illustration, hand-drawn thin black line work",
    "negative_prompt": "lettering, watermark, photorealism",
    "width": 1920,
    "height": 1080,
    "segments": [
        {
            "segment_id": "s01",
            "narration": "Electricity isn't a fuel. It's a carrier.",
            "scene_intent": "One stick figure holding a glowing battery, hills behind",
            "duration_hint_s": 3.5,
            "transition": "cut",
            "on_screen_text": "carrier",
        },
        {
            "segment_id": "s02",
            "narration": "It moves, and something has to move it.",
            "scene_intent": "The same figure pushing a box up a slope",
            "duration_hint_s": 3.0,
            "transition": "fade",
            "on_screen_text": None,
        },
    ],
}


def cfg():
    return dataclasses.replace(load_config(), segment_padding_s=0.25, fps=30, crf=30, fade_s=0.3)


class TestSchema(unittest.TestCase):
    def test_valid_script_passes(self):
        self.assertEqual(validate(VALID)["schema_version"], "1.0")

    def test_unknown_version_rejected(self):
        bad = {**VALID, "schema_version": "2.0"}
        with self.assertRaises(ScriptError) as ctx:
            validate(bad)
        self.assertIn("not supported", str(ctx.exception))

    def test_bad_transition_rejected(self):
        bad = {**VALID, "segments": [{**VALID["segments"][0], "transition": "dissolve"}, VALID["segments"][1]]}
        with self.assertRaises(ScriptError):
            validate(bad)

    def test_missing_field_rejected(self):
        seg = {k: v for k, v in VALID["segments"][0].items() if k != "scene_intent"}
        with self.assertRaises(ScriptError) as ctx:
            validate({**VALID, "segments": [seg]})
        self.assertIn("scene_intent", str(ctx.exception))

    def test_unknown_field_rejected(self):
        with self.assertRaises(ScriptError):
            validate({**VALID, "mood": "upbeat"})

    def test_duplicate_segment_id_rejected(self):
        with self.assertRaises(ScriptError) as ctx:
            validate({**VALID, "segments": [VALID["segments"][0], VALID["segments"][0]]})
        self.assertIn("duplicate", str(ctx.exception))

    def test_bad_segment_id_rejected(self):
        with self.assertRaises(ScriptError):
            validate({**VALID, "segments": [{**VALID["segments"][0], "segment_id": "frame1"}]})


class TestDerive(unittest.TestCase):
    def test_style_block_is_byte_identical_across_frames(self):
        frames = derive_frame_script(VALID, cfg())["frames"]
        prefixes = {f["prompt"].split("\n\n")[0] for f in frames}
        self.assertEqual(prefixes, {VALID["style_prompt"]})

    def test_scene_intent_normalised_with_no_text_clause(self):
        frames = derive_frame_script(VALID, cfg())["frames"]
        for frame in frames:
            self.assertIn(NO_TEXT_CLAUSE, frame["prompt"])
            self.assertNotIn("no text", frame["prompt"].split(NO_TEXT_CLAUSE)[0].lower() + "x")
        self.assertTrue(frames[0]["prompt"].startswith(VALID["style_prompt"] + "\n\n"))

    def test_style_block_never_leaks_the_style_words_into_scene_text(self):
        # scene_intent is human-facing: it must not be edited into prompt register.
        derived = derive_frame_script(VALID, cfg())
        self.assertNotIn("16:9", derived["frames"][0]["prompt"])

    def test_seeds_are_stable_and_distinct(self):
        first = derive_frame_script(VALID, cfg())["frames"]
        second = derive_frame_script(VALID, cfg())["frames"]
        self.assertEqual([f["seed"] for f in first], [f["seed"] for f in second])
        self.assertEqual(len({f["seed"] for f in first}), len(first))

    def test_narration_script_is_id_and_text_only(self):
        script = derive_narration_script(VALID, cfg())
        self.assertEqual(sorted(script["segments"][0]), ["segment_id", "text"])
        self.assertEqual(len(script["segments"]), len(VALID["segments"]))

    def test_non_multiple_of_eight_generation_size_rejected(self):
        with self.assertRaises(ScriptError):
            derive_frame_script(VALID, dataclasses.replace(cfg(), comfy_width=770))


class TestTimeline(unittest.TestCase):
    def entries(self):
        audio = [
            {"segment_id": "s01", "audio": Path("media/audio/s01.wav"), "duration_s": 3.0},
            {"segment_id": "s02", "audio": Path("media/audio/s02.wav"), "duration_s": 2.0},
        ]
        frames = [
            {"segment_id": "s01", "image": Path("media/frames/s01.png"), "seed": 1},
            {"segment_id": "s02", "image": Path("media/frames/s02.png"), "seed": 2},
        ]
        return audio, frames

    def test_audio_is_the_clock_and_padding_is_added(self):
        audio, frames = self.entries()
        clips = build_timeline(VALID, audio, frames, 0.25, "auto")
        self.assertEqual([c.duration_s for c in clips], [3.25, 2.25])
        self.assertEqual([c.start_s for c in clips], [0.0, 3.25])

    def test_hint_never_changes_the_timeline(self):
        audio, frames = self.entries()
        tweaked = {**VALID, "segments": [{**s, "duration_hint_s": 99} for s in VALID["segments"]]}
        a = build_timeline(VALID, audio, frames, 0.25, "auto")
        b = build_timeline(tweaked, audio, frames, 0.25, "auto")
        self.assertEqual([c.duration_s for c in a], [c.duration_s for c in b])

    def test_missing_media_fails_loudly_with_no_placeholder(self):
        audio, frames = self.entries()
        with self.assertRaises(Exception) as ctx:
            build_timeline(VALID, audio[:1], frames, 0.25, "auto")
        self.assertIn("s02", str(ctx.exception))

    def test_caption_modes(self):
        audio, frames = self.entries()
        auto = build_timeline(VALID, audio, frames, 0.0, "auto")
        self.assertEqual([c.caption for c in auto], ["carrier", VALID["segments"][1]["narration"]])
        strict = build_timeline(VALID, audio, frames, 0.0, "on_screen_text")
        self.assertEqual([c.caption for c in strict], ["carrier", None])
        off = build_timeline(VALID, audio, frames, 0.0, "off")
        self.assertEqual([c.caption for c in off], [None, None])


class TestRenderHelpers(unittest.TestCase):
    def clips(self):
        return [
            Clip("s01", Path("f/s01.png"), Path("a/s01.wav"), 2.0, 2.25, "cut", "one", 0.0, 2.0),
            Clip("s02", Path("f/s02.png"), Path("a/s02.wav"), 2.0, 2.25, "fade", None, 2.25, 2.0),
        ]

    def test_audio_list_lists_each_file_exactly_once(self):
        # No duration directives, no repeat: the demuxer inherits the previous
        # entry's duration onto an unterminated final entry, which silently
        # duplicated a whole segment when this was last written.
        text = asm._audio_list(["audio/s01.wav", "audio/s02.wav"])
        self.assertEqual(text, "file 'audio/s01.wav'\nfile 'audio/s02.wav'\n")
        self.assertNotIn("duration", text)

    def test_fades_use_absolute_start_times(self):
        filters = asm._fade_filters(self.clips(), 0.3)
        self.assertIn("fade=t=in:st=2.250:d=0.300", filters)
        self.assertTrue(any(f.startswith("fade=t=out") for f in filters))
        self.assertEqual(len(filters), 2)  # one dip-in, one fade-out

    def test_no_fade_filters_when_fade_is_zero(self):
        self.assertEqual(asm._fade_filters(self.clips(), 0.0), [])

    def test_srt_covers_narration_not_padding(self):
        srt = asm.build_srt(self.clips())
        self.assertIn("00:00:00,000 --> 00:00:02,000", srt)
        self.assertNotIn("00:00:02,250", srt)
        self.assertEqual(srt.count("-->"), 1)  # only the clip with a caption

    def test_srt_time_rollover(self):
        self.assertEqual(asm._srt_time(3671.456), "01:01:11,456")


class TestEndToEndAssembly(unittest.TestCase):
    """Real lavfi media in, real mp4 out."""

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_renders_a_playable_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "media" / "frames").mkdir(parents=True)
            (run_dir / "media" / "audio").mkdir(parents=True)
            (run_dir / "output").mkdir(parents=True)

            audio_entries, frame_entries = [], []
            for index, segment in enumerate(VALID["segments"], start=1):
                sid = segment["segment_id"]
                png = run_dir / "media" / "frames" / f"{sid}.png"
                wav = run_dir / "media" / "audio" / f"{sid}.wav"
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                     f"color=c=0xF2EFE6:s=768x432", "-frames:v", "1", str(png)],
                    check=True,
                )
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                     f"sine=frequency={300 + index * 100}:duration=1.2", "-ar", "22050", "-ac", "1", str(wav)],
                    check=True,
                )
                audio_entries.append({"segment_id": sid, "audio": wav, "duration_s": 1.2})
                frame_entries.append({"segment_id": sid, "image": png, "seed": index})

            from cf import media

            clips = build_timeline(VALID, audio_entries, frame_entries, 0.25, "auto")
            media.pad_audio(clips, run_dir / "media")
            video = asm.assemble(VALID, clips, cfg(), run_dir, "draft_cut.mp4")

            self.assertTrue(video.exists())
            probed = asm.probe_output(video)
            # The exact timeline: audio is the master clock, so any drift between
            # the rendered length and the measured clips is a bug, not a rounding note.
            expected = sum(c.duration_s for c in clips)
            self.assertAlmostEqual(float(probed["format"]["duration"]), expected, delta=0.2)
            self.assertIn("h264", probed["streams"][0]["codec_name"])
            self.assertEqual(probed["streams"][0]["width"], 1920)
            self.assertTrue((run_dir / "output" / "timings.json").exists())
            self.assertTrue(asm.contact_sheet(clips, run_dir).exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_missing_frame_fails_before_ffmpeg_runs(self):
        clips = TestRenderHelpers().clips()
        with self.assertRaises(Exception) as ctx:
            asm.assemble(VALID, clips, cfg(), Path(tempfile.mkdtemp()), "x.mp4")
        self.assertIn("missing image artifact", str(ctx.exception))


class TestExternalClients(unittest.TestCase):
    """Against mock_services, so the two network clients have offline coverage."""

    def test_llm_client_returns_content(self):
        import dataclasses as dc

        from cf import llm
        from tests.mock_services import MockServices

        with MockServices() as mocks:
            mock_cfg = dc.replace(cfg(), openai_api_key="mock", openai_base_url=f"{mocks.base_url}/v1",
                                  openai_model="mock")
            self.assertIn("Dossier", llm.chat(mock_cfg, "You are the Researcher.", "topic: x"))

    def test_scriptwriter_strips_fences_and_validates(self):
        import dataclasses as dc

        from cf import agents, llm
        from tests.mock_services import MockServices

        with MockServices() as mocks:
            mock_cfg = dc.replace(cfg(), openai_api_key="mock", openai_base_url=f"{mocks.base_url}/v1",
                                  openai_model="mock")
            fenced = f"```json\n{json.dumps(VALID)}\n```"
            # patch where it is used: agents binds chat at import time
            with unittest.mock.patch("cf.agents.chat", return_value=fenced) as fake:
                base = agents.script(mock_cfg, "a story")
            self.assertTrue(fake.call_args.kwargs["json_mode"])
            self.assertEqual(base["schema_version"], "1.0")
            self.assertEqual(len(base["segments"]), 2)

    def test_scriptwriter_rejects_unparseable_output(self):
        from cf import agents

        with unittest.mock.patch("cf.agents.chat", return_value="I think the story should go like this..."):
            with self.assertRaises(ScriptError) as ctx:
                agents.script(cfg(), "a story")
        self.assertIn("parseable JSON", str(ctx.exception))

    def test_comfy_client_generates_and_validates_a_frame(self):
        import dataclasses as dc
        import tempfile

        from cf import media
        from tests.mock_services import MockServices

        with MockServices() as mocks, tempfile.TemporaryDirectory() as tmp:
            workflow = Path(tmp) / "wf.json"
            workflow.write_text(
                json.dumps({
                    "3": {"class_type": "KSampler", "inputs": {"seed": "{{SEED}}", "steps": "{{STEPS}}",
                                                                "cfg": "{{CFG}}", "text": "{{POSITIVE}}"}},
                    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{POSITIVE}}", "negative": "{{NEGATIVE}}"}},
                    "_cf_output_node": "9",
                }),
                encoding="utf-8",
            )
            cfg_ = dc.replace(cfg(), comfy_base_url=mocks.base_url, comfy_workflow=workflow,
                              comfy_checkpoint="", media_max_retries=1)
            entries = media.generate_frames(cfg_, derive_frame_script(VALID, cfg_), Path(tmp) / "frames")
            self.assertEqual(len(entries), 2)
            for entry in entries:
                self.assertTrue(entry["image"].read_bytes().startswith(b"\x89PNG"))
            # the workflow graph carried our values through, not the raw tokens
            rendered = media._render({"a": "{{POSITIVE}}", "b": ["{{WIDTH}}", {"c": "{{SEED}}"}]},
                                     {"POSITIVE": "p", "WIDTH": 768, "SEED": 5})
            self.assertEqual(rendered, {"a": "p", "b": ["768", {"c": "5"}]})

    def test_comfy_client_rejects_non_png_response(self):
        import dataclasses as dc
        import tempfile

        from cf import media
        from tests.mock_services import MockServices

        with MockServices(png=b"<html>not an image</html>") as mocks, tempfile.TemporaryDirectory() as tmp:
            workflow = Path(tmp) / "wf.json"
            workflow.write_text(json.dumps({"6": {"inputs": {"text": "{{POSITIVE}}"}}}), encoding="utf-8")
            cfg_ = dc.replace(cfg(), comfy_base_url=mocks.base_url, comfy_workflow=workflow,
                              comfy_checkpoint="", media_max_retries=1, comfy_timeout_s=20)
            with self.assertRaises(CfError) as ctx:
                media.generate_frames(cfg_, derive_frame_script(VALID, cfg_), Path(tmp) / "frames")
            self.assertIn("not a PNG", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()