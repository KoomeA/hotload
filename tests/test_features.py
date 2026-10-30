import zipfile

import pytest

from hotload import engine
from hotload.config import Settings


def test_validate_lang_blocks_selector_injection():
    for ok in ("en", "pt-BR", "zh-Hans", "fil", "es-419"):
        assert engine.validate_lang(ok) == ok
    for bad in ("en]/x", "en;rm", "", "e", "english-us-extra-long", "en,fr", "en[x]"):
        with pytest.raises(ValueError):
            engine.validate_lang(bad)


def test_format_for_prefers_language_then_falls_back():
    assert engine.format_for(engine.PRESETS["mp3"], None) == "ba/b"
    f = engine.format_for(engine.PRESETS["720p"], "es")
    assert f.startswith("bv*[height<=720][ext=mp4]+ba[ext=m4a][language^=es]/")
    assert f.endswith("/" + engine.PRESETS["720p"].format)  # always falls back to the normal selection


def test_subtitle_options_file_vs_embed(tmp_path):
    s = Settings()
    file_mode = engine.build_download_opts(s, tmp_path, engine.PRESETS["720p"], subtitle_langs=["en", "fr"],
                                           subtitle_format="vtt", subtitle_mode="file")
    assert file_mode["writesubtitles"] and file_mode["subtitleslangs"] == ["en", "fr"]
    assert {"key": "FFmpegSubtitlesConvertor", "format": "vtt", "when": "before_dl"} in file_mode["postprocessors"]
    embed = engine.build_download_opts(s, tmp_path, engine.PRESETS["720p"], subtitle_langs=["en"], subtitle_mode="embed")
    assert {"key": "FFmpegEmbedSubtitle"} in embed["postprocessors"] and "subtitlesformat" not in embed
    only = engine.build_download_opts(s, tmp_path, engine.PRESETS["subtitles"], subtitle_langs=["en"], start=1, end=2)
    assert only["skip_download"] and only["format"] is None and "download_ranges" not in only
    with pytest.raises(engine.EngineError):
        engine.build_download_opts(s, tmp_path, engine.PRESETS["subtitles"])
    with pytest.raises(ValueError):
        engine.build_download_opts(s, tmp_path, engine.PRESETS["720p"], subtitle_langs=["en]/x"])


def test_package_single_file_zip_and_ignored_artifacts(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"video")
    (tmp_path / "v.jpg").write_bytes(b"thumb")
    (tmp_path / "v.mp4.part").write_bytes(b"x")
    assert engine.package(tmp_path).name == "v.mp4"
    (tmp_path / "v.en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    (tmp_path / "v.fr.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nsalut\n")
    z = engine.package(tmp_path)
    assert z.suffix == ".zip"
    assert sorted(zipfile.ZipFile(z).namelist()) == ["v.en.srt", "v.fr.srt", "v.mp4"]
    assert engine.package(tmp_path, keep_subs=False).name == "v.mp4"  # media only; the old archive is ignored


def test_package_subs_only_single_file(tmp_path):
    (tmp_path / "x.en.srt").write_text("s")
    assert engine.package(tmp_path).name == "x.en.srt"
    with pytest.raises(engine.EngineError):
        engine.package(tmp_path, keep_subs=False)


def test_summarise_languages():
    info = {"formats": [
        {"acodec": "aac", "language": "en", "vcodec": "none"},
        {"acodec": "aac", "language": "es", "vcodec": "none"},
        {"acodec": "none", "language": "de", "vcodec": "avc1", "height": 720},
        {"acodec": "aac", "language": "evil]/x"},
    ], "subtitles": {"fr": [], "en": []}, "automatic_captions": {"de": []}}
    s = engine.summarise(info)
    assert s["audio_langs"] == ["en", "es"] and s["subtitles"] == ["en", "fr"] and s["auto_captions"] == ["de"]
