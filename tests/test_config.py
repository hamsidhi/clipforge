import pytest

from clipforge.config import Config, ConfigError, load_config, mask_secret
from clipforge.utils.ffmpeg import escape_filter_path
from clipforge.workspace import video_id_for


def test_repo_config_loads():
    cfg = load_config()
    assert cfg.clips.count == 20
    assert cfg.captions.script == "roman"
    assert cfg.render.width == 1080 and cfg.render.height == 1920


def test_invalid_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("clips: {min_sec: 60, max_sec: 30}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
    p.write_text("clips: {unknown_key: 1}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_overrides_skip_none():
    cfg = Config().with_overrides({"clips.count": 15, "llm.provider": None})
    assert cfg.clips.count == 15 and cfg.llm.provider == "ollama"


def test_mask_secret():
    assert mask_secret(None) is None
    assert mask_secret("abcdefghijkl").startswith("abcd") and "efgh" not in mask_secret("abcdefghijkl")


def test_video_id():
    assert video_id_for("https://www.youtube.com/watch?v=sdMHVIcPGwg") == "sdMHVIcPGwg"
    assert video_id_for("https://youtu.be/sdMHVIcPGwg?t=3") == "sdMHVIcPGwg"
    assert video_id_for("https://youtu.be/sdMHVIcPGwg", (600, 1200)) == "sdMHVIcPGwg_600-1200"


def test_escape_filter_path_windows_drive(tmp_path):
    p = escape_filter_path(tmp_path / "a b's.ass")
    assert "\\" not in p.replace("\\:", "").replace("\\'", "")
    if ":" in str(tmp_path):
        assert "\\:" in p
