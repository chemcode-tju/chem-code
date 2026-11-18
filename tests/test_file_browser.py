"""Live file reads must preserve account, directory and SSH selection boundaries."""

import httpx
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.auth import create_user_account
from chemistry_agent.file_browser import PREVIEW_BYTES


def test_local_files_are_listed_and_previewed_without_publishing(settings_factory, tmp_path):
    settings = settings_factory(ssh_hosts="cluster-a,cluster-b")
    project = tmp_path / "Cu studies"
    project.mkdir()
    relax = project / "01-relax"
    relax.mkdir()
    name = "Cu & O.xyz"
    structure = b"2\nCu O\nCu 0 0 0\nO 0 0 1.8\n"
    (relax / name).write_bytes(structure)
    (project / "OUTCAR").write_bytes(b"a" * (PREVIEW_BYTES - 1) + "中\n".encode())
    (project / "WAVECAR").write_bytes(b"\x00\x01")
    (project / "linked").symlink_to(relax, target_is_directory=True)
    (project / "outside").symlink_to(tmp_path, target_is_directory=True)
    (project / ".private").write_text("hidden")

    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
    ) as client:
        base = "/api/file-browser"
        assert [item["id"] for item in client.get(f"{base}/sources").json()["sources"]] == [
            "workspace",
            "local",
            "ssh:cluster-a",
            "ssh:cluster-b",
        ]
        location = {"source": "local", "root": str(project)}
        response = client.get(f"{base}/directory", params=location)
        assert response.is_success, response.text
        assert response.json()["entries"] == [
            {"kind": "directory", "name": "01-relax"},
            {"kind": "directory", "name": "linked"},
            {"kind": "file", "name": "OUTCAR"},
            {"kind": "file", "name": "WAVECAR"},
        ]
        text = client.get(f"{base}/preview", params=location | {"path": "OUTCAR"}).json()
        assert (text["kind"], text["truncated"], len(text["text"])) == (
            "text",
            True,
            PREVIEW_BYTES - 1,
        )
        binary = client.get(f"{base}/preview", params=location | {"path": "WAVECAR"}).json()
        assert binary == {"kind": "binary", "size": 2, "downloadable": True}
        matches = client.get(f"{base}/search", params=location | {"q": "CU & O"})
        assert matches.json() == {
            "files": [{"kind": "file", "name": name, "path": f"01-relax/{name}"}],
            "limited": False,
        }
        original = client.get(f"{base}/content", params=location | {"path": f"linked/{name}"})
        assert original.content == structure
        assert original.headers["content-type"] == "application/octet-stream"
        assert original.headers["content-disposition"].startswith("attachment;")
        for path in [f"../{name}", "/tmp/OUTCAR", ".private", "archive/OUTCAR", "outside"]:
            assert (
                client.get(f"{base}/content", params=location | {"path": path}).status_code == 403
            )
        assert client.get(f"{base}/directory", params={"source": "ssh:other"}).status_code == 403


async def test_shared_file_browser_only_exposes_the_callers_workspace(settings_factory):
    settings = settings_factory(auth_enabled=True)
    password = "test-file-browser-password"
    await create_user_account(settings, "alice", password)
    await create_user_account(settings, "bob", password)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as alice,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as bob,
    ):
        base = "/api/file-browser"
        assert (await alice.get(f"{base}/sources")).status_code == 401
        for client, name in [(alice, "alice"), (bob, "bob")]:
            await client.post("/auth/login", json={"username": name, "password": password})
        session = (await alice.post("/sessions", json={})).json()["id"]
        params = {"source": "workspace", "session": session}
        assert (await alice.get(f"{base}/directory", params=params)).json()["entries"] == []
        workspace = app.state.workspace_files.workspace(session)
        workspace.mkdir(parents=True)
        (workspace / "INCAR").write_text("ENCUT = 520\n")
        (workspace / "linked").symlink_to(workspace / "INCAR")
        sources = (await alice.get(f"{base}/sources")).json()["sources"]
        assert [source["id"] for source in sources] == ["workspace"]
        assert (await alice.get(f"{base}/preview", params=params | {"path": "INCAR"})).json()[
            "text"
        ] == "ENCUT = 520\n"
        assert (
            await bob.get(f"{base}/content", params=params | {"path": "INCAR"})
        ).status_code == 404
        assert (await bob.get(f"{base}/search", params=params | {"q": "INCAR"})).status_code == 404
        assert (
            await alice.get(f"{base}/content", params=params | {"path": "linked"})
        ).status_code == 403
        assert (
            await alice.get(f"{base}/directory", params=params | {"root": "/tmp"})
        ).status_code == 403
        assert (await alice.get(f"{base}/directory", params={"source": "local"})).status_code == 403
        frame = b"1\nframe\nCu 0 0 0\n"
        uploaded = await alice.post(
            "/api/files", headers={"X-File-Name": "sample.xyz"}, content=frame * 2
        )
        file_id = uploaded.json()["id"]
        attachment = {"source": f"attachment:{file_id}", "path": "sample.xyz"}
        assert (await alice.get(f"{base}/preview", params=attachment)).json()["text"].encode() == (
            frame * 2
        )
        first = await alice.get(
            f"{base}/trajectory", params=attachment | {"first": True, "index": True}
        )
        assert first.json()["text"].encode() == frame
        assert (await bob.get(f"{base}/preview", params=attachment)).status_code == 404
        assert (
            await alice.get(f"{base}/content", params=attachment | {"path": "different.xyz"})
        ).status_code == 404

        # Older attachments keep their original bytes in SQLite.
        async with app.state.store.transaction():
            await app.state.store.db.execute(
                "UPDATE attachments SET on_disk=0,content=? WHERE id=?", (frame * 2, file_id)
            )
        assert (await alice.get(f"{base}/content", params=attachment)).content == frame * 2
        first = await alice.get(
            f"{base}/trajectory", params=attachment | {"first": True, "index": True}
        )
        assert first.json()["text"].encode() == frame


def test_preview_media_range_and_utf16(settings_factory, tmp_path):
    pdf = b"%PDF-1.7\n1234567890"
    (tmp_path / "sample.pdf").write_bytes(pdf)
    (tmp_path / "sample.csv").write_bytes("编号,能量\n001,-0.25\n".encode("utf-16"))
    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
    ) as client:
        base = "/api/file-browser"
        location = {"source": "local", "root": str(tmp_path)}
        params = location | {"path": "sample.pdf"}
        partial = client.get(f"{base}/media", params=params, headers={"Range": "bytes=9-12"})
        assert partial.status_code == 206
        assert partial.content == b"1234"
        assert partial.headers["accept-ranges"] == "bytes"
        assert partial.headers["content-range"] == f"bytes 9-12/{len(pdf)}"
        assert partial.headers["content-type"] == "application/pdf"
        assert partial.headers["content-disposition"].startswith("attachment;")
        beyond = client.get(f"{base}/media", params=params, headers={"Range": "bytes=80-99"})
        assert beyond.status_code == 416
        assert beyond.headers["content-range"] == f"bytes */{len(pdf)}"
        text = location | {"path": "sample.csv"}
        assert client.get(f"{base}/media", params=text).status_code == 415
        assert client.get(f"{base}/preview", params=text).json()["text"] == "编号,能量\n001,-0.25\n"


def test_xyz_trajectory_indexes_complete_frames_and_freezes_append_boundary(
    settings_factory, tmp_path
):
    path = tmp_path / "positions.extxyz"
    frame = "2\r\n金属表面\r\nCu 0 0 0\r\nO 0 0 1.8\r\n".encode()
    tail = b"2\nappending\nCu 0 0 0\nO 0"
    path.write_bytes(frame * 2 + tail)

    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
    ) as client:
        endpoint = "/api/file-browser/trajectory"
        params = {"source": "local", "root": str(tmp_path), "path": path.name}
        opened = client.get(endpoint, params=params | {"first": True, "index": True, "length": 8})
        assert opened.is_success, opened.text
        first = opened.json()
        assert first["spans"] == [[0, len(frame)]]
        assert first["text"].encode() == frame
        size = path.stat().st_size
        assert first["size"] == size

        with path.open("ab") as writer:
            writer.write(b" 0 1.9\n" + frame)

        frozen = params | {"snapshot": first["snapshot"], "offset": first["nextOffset"]}
        rest = client.get(endpoint, params=frozen | {"index": True}).json()
        assert rest["spans"] == [[len(frame), len(frame) * 2]]
        assert rest["nextOffset"] == size
        second = client.get(endpoint, params=frozen | {"length": len(frame)})
        assert second.content == frame
        reopened = client.get(endpoint, params=params | {"index": True}).json()
        assert len(reopened["spans"]) == 4

        replacement = tmp_path / "replacement.extxyz"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        assert client.get(endpoint, params=frozen).status_code == 409


def test_large_binary_trajectory_reads_a_small_range(settings_factory, tmp_path):
    path = tmp_path / "positions.traj"
    offset = 160 * 1024 * 1024
    with path.open("wb") as file:
        file.seek(offset)
        file.write(b"last block")

    with TestClient(
        create_app(settings_factory()), base_url="http://127.0.0.1", client=("127.0.0.1", 5000)
    ) as client:
        endpoint = "/api/file-browser/trajectory"
        params = {"source": "local", "root": str(tmp_path), "path": path.name, "length": 10}
        initial = client.get(endpoint, params=params)
        assert initial.headers["x-trajectory-size"] == str(offset + 10)
        requested = params | {
            "snapshot": initial.headers["x-trajectory-snapshot"],
            "offset": offset,
        }
        assert client.get(endpoint, params=requested).content == b"last block"
        with path.open("ab") as file:
            file.write(b"appended")
        assert client.get(endpoint, params=requested).status_code == 409
