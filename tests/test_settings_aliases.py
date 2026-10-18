"""DeepSeek configuration compatibility and source precedence, no real keys."""

import pytest

from app.config import Settings, get_settings


FIELDS = [
    ("deepseek_api_key", "groq_api_key", "", "controlled-new-key", "controlled-old-key"),
    ("deepseek_model", "groq_model", "deepseek-flash", "controlled-new-model", "controlled-old-model"),
    ("deepseek_max_context_tokens", "groq_max_context_tokens", 90_000, 71_000, 62_000),
    ("deepseek_default_max_completion_tokens", "groq_default_max_completion_tokens", 2_048, 1_100, 1_200),
]


@pytest.fixture(autouse=True)
def clean_model_inputs(monkeypatch):
    for field, legacy, *_ in FIELDS:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(legacy.upper(), raising=False)


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
def test_defaults_are_preserved(field, legacy, default, new, old):
    assert getattr(Settings(_env_file=None), field) == default


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
@pytest.mark.parametrize("use_legacy", [False, True])
def test_constructor_accepts_each_name(field, legacy, default, new, old, use_legacy):
    settings = Settings(_env_file=None, **{legacy if use_legacy else field: new})
    assert getattr(settings, field) == new


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
@pytest.mark.parametrize("use_legacy", [False, True])
def test_environment_accepts_each_name(monkeypatch, field, legacy, default, new, old, use_legacy):
    monkeypatch.setenv((legacy if use_legacy else field).upper(), str(new))
    assert getattr(Settings(_env_file=None), field) == new


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
@pytest.mark.parametrize("use_legacy", [False, True])
def test_dotenv_accepts_each_name(tmp_path, field, legacy, default, new, old, use_legacy):
    dotenv = tmp_path / "controlled.env"
    dotenv.write_text(f"{(legacy if use_legacy else field).upper()}={new}\n", encoding="utf-8")
    assert getattr(Settings(_env_file=dotenv), field) == new


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
@pytest.mark.parametrize("source", ["constructor", "environment", "dotenv"])
def test_new_name_wins_within_one_source(monkeypatch, tmp_path, field, legacy, default, new, old, source):
    kwargs = {}
    dotenv = None
    if source == "constructor":
        kwargs = {legacy: old, field: new}
    elif source == "environment":
        monkeypatch.setenv(legacy.upper(), str(old))
        monkeypatch.setenv(field.upper(), str(new))
    else:
        dotenv = tmp_path / "controlled.env"
        dotenv.write_text(f"{legacy.upper()}={old}\n{field.upper()}={new}\n", encoding="utf-8")
    assert getattr(Settings(_env_file=dotenv, **kwargs), field) == new


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
@pytest.mark.parametrize("environment_legacy", [False, True])
@pytest.mark.parametrize("dotenv_legacy", [False, True])
def test_source_priority_precedes_alias_preference(monkeypatch, tmp_path, field, legacy, default, new, old,
                                                  environment_legacy, dotenv_legacy):
    dotenv = tmp_path / "controlled.env"
    dotenv.write_text(f"{(legacy if dotenv_legacy else field).upper()}={new}\n", encoding="utf-8")
    monkeypatch.setenv((legacy if environment_legacy else field).upper(), str(old))
    assert getattr(Settings(_env_file=dotenv), field) == old


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
@pytest.mark.parametrize("constructor_legacy", [False, True])
def test_constructor_precedes_both_lower_sources(monkeypatch, tmp_path, field, legacy, default, new, old,
                                                constructor_legacy):
    dotenv = tmp_path / "controlled.env"
    dotenv.write_text(f"{field.upper()}={old}\n{legacy.upper()}={old}\n", encoding="utf-8")
    monkeypatch.setenv(field.upper(), str(old))
    monkeypatch.setenv(legacy.upper(), str(old))
    settings = Settings(_env_file=dotenv, **{legacy if constructor_legacy else field: new})
    assert getattr(settings, field) == new


@pytest.mark.parametrize("field,legacy,default,new,old", FIELDS)
def test_copy_uses_the_canonical_field(field, legacy, default, new, old):
    original = Settings(_env_file=None, **{legacy: old})
    copied = original.model_copy(update={field: new})
    assert getattr(copied, field) == new
    assert getattr(original, field) == old


def test_key_remains_hidden_in_repr_and_empty_preferred_input_is_not_replaced():
    settings = Settings(_env_file=None, deepseek_api_key="controlled-private-placeholder")
    assert "controlled-private-placeholder" not in repr(settings)
    assert Settings(_env_file=None, deepseek_api_key="", groq_api_key="controlled-old").deepseek_api_key == ""


def test_settings_cache_reload_applies_canonical_environment(monkeypatch):
    get_settings.cache_clear()
    try:
        monkeypatch.setenv("GROQ_MODEL", "controlled-old-model")
        original = get_settings()
        monkeypatch.setenv("DEEPSEEK_MODEL", "controlled-new-model")
        assert get_settings() is original
        get_settings.cache_clear()
        assert get_settings().deepseek_model == "controlled-new-model"
    finally:
        get_settings.cache_clear()
