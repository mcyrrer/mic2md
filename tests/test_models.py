import pytest

from mic2md import models


def test_default_models():
    en = models.get_spec("en")
    assert en.url.endswith("ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin")
    sv = models.get_spec("sv")
    assert (
        sv.url == "https://huggingface.co/KBLab/kb-whisper-large/resolve/main/ggml-model-q5_0.bin"
    )
    assert sv.local_file == "ggml-kb-whisper-large-q5_0.bin"


def test_unknown_size_and_language():
    with pytest.raises(ValueError, match="Choose from"):
        models.get_spec("sv", "base.en")
    with pytest.raises(ValueError, match="Unsupported language"):
        models.get_spec("de")


def test_cached_model_is_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setenv("MIC2MD_CACHE_DIR", str(tmp_path))
    spec = models.get_spec("en", "base.en")
    (tmp_path / spec.local_file).write_bytes(b"x")
    monkeypatch.setattr(models, "download", lambda *a: pytest.fail("should not download"))
    assert models.ensure_model(spec) == tmp_path / spec.local_file
