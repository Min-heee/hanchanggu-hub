import os

from hub.app import load_env_file


def test_env_file_fills_missing_values_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# 주석\nHUB_TEST_A="from-file"\nHUB_TEST_B=from-file\nHUB_TEST_EMPTY=\n', encoding="utf-8")
    monkeypatch.delenv("HUB_TEST_A", raising=False)
    monkeypatch.setenv("HUB_TEST_B", "from-shell")
    monkeypatch.delenv("HUB_TEST_EMPTY", raising=False)

    load_env_file(env)

    assert os.environ["HUB_TEST_A"] == "from-file"
    assert os.environ["HUB_TEST_B"] == "from-shell"
    assert "HUB_TEST_EMPTY" not in os.environ
