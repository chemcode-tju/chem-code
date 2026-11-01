import io
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("aiida")
from chemistry_agent.automation import backend  # noqa: E402
from chemistry_agent.automation.task_files import relative_path, stage_files, task_files  # noqa: E402
from chemistry_agent.errors import AgentError  # noqa: E402


class Outputs(dict):
    def __getattr__(self, name):
        return self[name]


class Repository:
    def __init__(self):
        self.files = {"custom.bin": b"\x00\xffbinary", "extra/numbers.dat": b"123456789"}

    def open(self, name, mode):
        return io.BytesIO(self.files[name])

    def list_objects(self, path):
        names = ["custom.bin", "extra"] if not path else ["numbers.dat"]
        return [SimpleNamespace(name=name, is_dir=lambda n=name: n == "extra") for name in names]

    def walk(self):
        return [(Path("."), ["extra"], ["custom.bin"]), (Path("extra"), [], ["numbers.dat"])]


def test_arbitrary_retrieved_files_staging_and_ranges(tmp_path, monkeypatch):
    repo = Repository()
    node = SimpleNamespace(
        uuid="test",
        outputs=Outputs(retrieved=SimpleNamespace(base=SimpleNamespace(repository=repo))),
    )
    monkeypatch.setattr(backend, "load_job", lambda _: node)
    monkeypatch.setenv("AIIDA_PATH", str(tmp_path))
    listing = task_files({"job_id": "1"})
    assert {item["name"] for item in listing["entries"]} == {"custom.bin", "extra"}
    result = task_files(
        {"job_id": "1", "mode": "read", "path": "extra/numbers.dat", "offset": 2, "length": 3}
    )
    assert result["content"] == "345" and result["next_offset"] == 5
    destination = tmp_path / "chat" / "session" / "analysis"
    result = stage_files({"job_id": "1", "files": ["*"], "directory": str(destination)})
    assert Path(result["files"]["custom.bin"]).read_bytes() == b"\x00\xffbinary"
    assert Path(result["files"]["extra/numbers.dat"]).read_text() == "123456789"
    with pytest.raises(AgentError):
        stage_files(
            {"job_id": "1", "files": ["custom.bin"], "directory": str(tmp_path / "outside")}
        )
    for path in ["../credentials", "/etc/passwd", "archive/old", "bad\x00file"]:
        with pytest.raises(AgentError):
            relative_path(path)
