import pytest

from chemistry_agent.config import Settings


@pytest.fixture
def settings_factory(tmp_path):
    def create(**overrides):
        values = {
            "_env_file": None,
            "deepseek_api_key": "test-only-key",
            "deepseek_base_url": "https://deepseek.test/v1",
            "deepseek_model": "deepseek-flash",
            "deepseek_thinking": False,
            "database_path": tmp_path / "agent.sqlite3",
            "tokenizer_path": tmp_path / "missing-tokenizer.json",
            "aiida_path": tmp_path / "aiida",
            "automation_monitor_enabled": False,
        }
        if overrides.get("automation_enabled"):
            # These integration tests use the byte-per-token fallback, not a tokenizer.
            # Leave room for the growing automation tool schemas; context tests set their own limit.
            values["context_window_tokens"] = 65536
        return Settings(**(values | overrides))

    return create
