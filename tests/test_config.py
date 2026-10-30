from pathlib import Path

import pytest

from tagdrifter.config import load_settings


def test_defaults_without_config(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = load_settings(env={})
    assert s.case_sensitive is True
    assert s.audio_extensions == (".flac",)


def test_toml_env_and_override_precedence(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[library]
path = "/from/toml"
case_sensitive = false
audio_extensions = ["FLAC", ".ogg"]
[database]
path = "/toml.db"
[checks]
disabled = ["path.loose_file"]
[checks."tags.required"]
tags = ["title"]
""",
        encoding="utf-8",
    )
    s = load_settings(
        cfg,
        env={"TAGDRIFTER_DB": "/env.db", "TAGDRIFTER_CASE_SENSITIVE": "yes"},
        library_path=Path("/override"),
    )
    assert s.library_path == Path("/override")
    assert s.database_path == Path("/env.db")
    assert s.case_sensitive is True
    assert s.audio_extensions == (".flac", ".ogg")
    assert s.disabled_checks == {"path.loose_file"}
    assert s.options_for("tags.required") == {"tags": ["title"]}
    assert s.options_for("other") == {}


def test_config_from_env_var(tmp_path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[library]\npath = "/x"\n', encoding="utf-8")
    assert load_settings(env={"TAGDRIFTER_CONFIG": str(cfg)}).library_path == Path("/x")


def test_missing_explicit_config(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.toml", env={})


def test_bad_bool(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        load_settings(env={"TAGDRIFTER_CASE_SENSITIVE": "maybe"})


def test_scan_schedule_from_toml_and_env(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[scan]
schedule = "15 3 * * *"
""",
        encoding="utf-8",
    )
    assert load_settings(cfg, env={}).scan_schedule == "15 3 * * *"
    assert load_settings(cfg, env={"TAGDRIFTER_SCHEDULE": "off"}).scan_schedule == "off"
    monkeypatch.chdir(tmp_path / "..")  # no config.toml here
    assert load_settings(env={}).scan_schedule == "0 4 * * *"
