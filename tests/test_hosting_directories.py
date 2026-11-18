"""Directory setup uses the configured host, with explicit filesystem mutations."""

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.file_browser import BrowserConnections
from chemistry_agent.hosting import routes


def test_local_directory_creation_validates_name_and_parent(settings_factory, tmp_path: Path):
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 1)
    ) as client:
        endpoint = "/api/hosting/hosts/local/directories"
        name = "Cu 111"
        destination = tmp_path / name
        assert client.get(endpoint, params={"path": str(destination)}).json()["exists"] is False
        result = client.post(endpoint, json={"parent": str(tmp_path), "name": name})
        assert result.status_code == 201
        assert result.json()["path"] == str(destination.resolve())
        assert destination.is_dir()
        duplicate = client.post(endpoint, json={"parent": str(tmp_path), "name": name})
        assert duplicate.status_code == 409

        for invalid in ("../escape", "/", ".", "archive", "Archive"):
            result = client.post(endpoint, json={"parent": str(tmp_path), "name": invalid})
            assert result.status_code == 422

        missing = tmp_path / "missing"
        assert client.post(endpoint, json={"parent": str(missing), "name": name}).status_code == 422
        assert not missing.exists()
        protected = client.post(
            endpoint,
            json={"parent": str(tmp_path), "name": "new"},
            headers={"origin": "https://other.example"},
        )
        assert protected.status_code == 403


def test_folder_picker_exposes_availability_and_cancellation(settings_factory, monkeypatch):
    async def cancel(path: str) -> None:
        assert path == "~/projects"
        return None

    monkeypatch.setattr(routes, "desktop_picker_available", lambda: True)
    monkeypatch.setattr(routes, "choose_folder", cancel)

    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 1)
    ) as client:
        assert client.get("/api/hosting").json()["desktop_folder_picker"] is True
        endpoint = "/api/hosting/hosts/local/folder-picker"
        assert client.post(endpoint, json={"path": "~/projects"}).json() == {"path": None}
        assert client.post(endpoint, json={"path": "../outside"}).status_code == 422
        blocked = client.post(
            endpoint, json={"path": "~/projects"}, headers={"origin": "https://other.example"}
        )
        assert blocked.status_code == 403


def test_remote_picker_tree_and_job_share_sftp(settings_factory, monkeypatch):
    ssh = pytest.importorskip("asyncssh")
    connections, scans, commands = [], [], []
    folders = {"old"}

    class Connection:
        def is_closed(self):
            return False

        def close(self):
            pass

        async def wait_closed(self):
            pass

        async def run(self, command, *, input, **kwargs):
            commands.append(input)
            return SimpleNamespace(exit_status=0, stdout='{"directory":"/project"}')

    class Files:
        async def realpath(self, path):
            return "/project" if str(path) == "." else str(path)

        async def stat(self, path):
            assert path == PurePosixPath("/project/old")
            return SimpleNamespace(type=ssh.FILEXFER_TYPE_DIRECTORY)

        async def mkdir(self, path):
            assert path.parent == PurePosixPath("/project")
            folders.add(path.name)

        async def scandir(self, path):
            assert path == PurePosixPath("/project")
            scans.append(path)
            for name in folders:
                yield SimpleNamespace(
                    filename=name, attrs=SimpleNamespace(type=ssh.FILEXFER_TYPE_DIRECTORY)
                )
            yield SimpleNamespace(
                filename="output.csv",
                attrs=SimpleNamespace(type=ssh.FILEXFER_TYPE_REGULAR, size=12),
            )

    async def connect(self, module, alias, profile):
        connections.append((alias, profile))
        return Connection(), Files()

    monkeypatch.setattr(BrowserConnections, "connect", connect)
    settings = settings_factory(ssh_hosts="cluster-b", automation_enabled=True)
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 1)
    ) as client:
        picker = "/api/hosting/hosts/cluster-b/directories"
        assert client.get(picker).json()["directories"] == [{"name": "old", "path": "/project/old"}]
        confirmed = client.get(picker, params={"path": "/project/old", "children": False})
        assert confirmed.json() == {"path": "/project/old", "exists": True}
        new = client.post(picker, json={"parent": "/project", "name": "new"})
        assert new.status_code == 201, new.text
        tree = "/api/file-browser/directory"
        result = client.get(tree, params={"source": "ssh:cluster-b", "root": "/project"})
        assert [entry["name"] for entry in result.json()["entries"]] == ["new", "old", "output.csv"]
        job = {"source": "job:cluster-b", "job_id": "42", "submitted": "2026-11-18T10:00:00"}
        for _ in range(3):
            assert client.get(tree, params=job).status_code == 200
        assert len(scans) == 5  # Confirming a folder does not list its children.
        assert connections == [("cluster-b", None)]
        assert len(commands) == 1  # A burst of job reads reuses the validated directory.
