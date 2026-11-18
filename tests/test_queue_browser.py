"""Queue browsing binds file reads to the account's current Slurm job identity."""

import base64
import subprocess

from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.automation import queue_remote
from chemistry_agent.environments.registry import Environment, Environments
from chemistry_agent.environments.runner import RunnerOperations


def test_monitor_reads_live_job_directory_and_rejects_other_paths(settings_factory, monkeypatch):
    settings = settings_factory(automation_enabled=True)
    directory = settings.platform_path.parent / "research run"
    directory.mkdir(parents=True)
    geometry = b"2\nH2\nH 0 0 0\nH 0 0 0.74\n"
    (directory / "final.xyz").write_bytes(geometry)
    (directory / "images").mkdir()
    (directory / "images" / "POSCAR").write_bytes(b"atom positions")
    (directory / "outside").symlink_to(settings.platform_path.parent)
    (directory / ".hidden").write_text("private")
    (directory / "energies.csv").write_text("step,energy\n1,-1.2\n")
    (directory / "spectrum.pdf").write_bytes(b"%PDF-1.4 diagnostic")

    registry = Environments(settings.platform_path)
    registry.save(Environment(id="local-hpc", name="Local Slurm", transport="local"))

    owner = queue_remote.pwd.getpwuid(queue_remote.os.getuid()).pw_name
    submitted = "2026-11-18T12:00:00"
    rows = [
        ("12_3", owner, "low", "RUNNING", "1:11", submitted, str(directory), "optimize | OH"),
        ("13", "different-account", "low", "RUNNING", "0:10", submitted, str(directory), "other"),
        ("14", owner, "low", "FAILED", "0:02", submitted, str(directory), "terminal"),
    ]

    def squeue(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        if command[0] == "sinfo":
            return subprocess.CompletedProcess(command, 0, "batch\nlow\nlow\n", "")
        if command[0] == "sacct":
            fields = (
                "12_3",
                owner,
                "batch",
                "COMPLETED",
                "1:11",
                submitted,
                "2026-11-18T12:01:11",
                str(directory),
                "finished",
            )
            return subprocess.CompletedProcess(command, 0, "|".join(fields), "")
        assert command[0] == "squeue"
        assert command[command.index("-u") + 1] == owner
        stdout = "\n".join("\x1f".join(row) for row in rows)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(queue_remote.subprocess, "run", squeue)
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 1)
    ) as client:
        assert client.get("/monitor").status_code == 200
        assert client.get("/api/job-monitor/hosts").json()["hosts"] == [
            {"id": "local-hpc", "name": "Local Slurm"}
        ]
        base = "/api/job-monitor/local-hpc/jobs"
        active = client.get(base).json()
        assert active["partitions"] == ["batch", "low"]
        jobs = active["jobs"]
        historical = client.get(base, params={"history": True}).json()["jobs"]
        assert [(row["status"], row["ended"]) for row in historical] == [
            ("COMPLETED", "2026-11-18T12:01:11")
        ]
        browser = "/api/file-browser"
        params = {"source": "job:local-hpc", "job_id": "12_3", "submitted": submitted}
        response = client.get(
            f"{browser}/content", params=params | {"path": "final.xyz", "history": True}
        )
        assert response.content == geometry
        first = client.get(
            f"{browser}/trajectory",
            params=params | {"path": "final.xyz", "index": True, "first": True},
        )
        assert first.json()["text"].encode() == geometry
        ranged = client.get(
            f"{browser}/trajectory",
            params=params
            | {"path": "final.xyz", "offset": 2, "snapshot": first.json()["snapshot"]},
        )
        assert ranged.content == geometry[2:]
        assert [(row["id"], row["name"]) for row in jobs] == [("12_3", "optimize | OH")]
        files = client.get(f"{browser}/directory", params=params).json()
        assert [entry["name"] for entry in files["entries"]] == [
            "images",
            "energies.csv",
            "final.xyz",
            "spectrum.pdf",
        ]
        nested = client.get(f"{browser}/directory", params=params | {"path": "images"})
        assert [entry["name"] for entry in nested.json()["entries"]] == ["POSCAR"]
        preview = client.get(f"{browser}/preview", params=params | {"path": "energies.csv"})
        assert preview.json()["text"] == "step,energy\n1,-1.2\n"
        pdf = client.get(
            f"{browser}/media",
            params=params | {"path": "spectrum.pdf"},
            headers={"Range": "bytes=0-7"},
        )
        assert pdf.status_code == 206 and pdf.content == b"%PDF-1.4"
        for path in ("../other", "outside/file", "archive/file", ".hidden"):
            rejected = client.get(f"{browser}/content", params=params | {"path": path})
            assert rejected.status_code in (403, 404)
        assert (
            client.get(f"{browser}/directory", params=params | {"submitted": "older"}).status_code
            != 200
        )
        assert (
            client.get(f"{browser}/directory", params=params | {"job_id": "13"}).status_code != 200
        )
        assert client.get(f"{browser}/directory", params=params | {"root": "/etc"}).json() == files
        assert (
            client.get(
                f"{browser}/directory", params=params, headers={"Origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/job-monitor/missing/jobs").status_code == 422
        assert client.get(base, headers={"Origin": "https://other.example"}).status_code == 403


def test_lammps_discovers_nested_data_and_latest_complete_frame(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "structure data.data").write_text(
        "LAMMPS data\n\n1 atom types\n\nMasses\n\n1 63.546 # Cu\n\nAtoms # atomic\n\n1 1 0 0 0\n"
    )
    (tmp_path / "incorrect.data").write_text("Masses\n\n1 12.011 # C\n")
    (tmp_path / "in.trajectory").write_text(
        'units metal\ninclude inputs/system.in\nread_data "inputs/structure data.data"\n'
    )
    (inputs / "system.in").write_text("boundary p p p\n")
    parts = {
        item["name"]: item["text"]
        for item in queue_remote.lammps_metadata(tmp_path, "trajectory.lammpstrj")
    }
    assert "63.546 # Cu" in parts["inputs/structure data.data"]
    assert "Atoms # atomic" not in parts["inputs/structure data.data"]
    assert parts["inputs/system.in"] == "boundary p p p"
    assert "incorrect.data" not in parts
    frame = (
        b"ITEM: TIMESTEP\n100\nITEM: NUMBER OF ATOMS\n1\n"
        b"ITEM: BOX BOUNDS pp pp pp\n0 5\n0 5\n0 5\nITEM: ATOMS id type x y z\n1 1 0 0 0\n"
    )
    dump = tmp_path / "trajectory.lammpstrj"
    dump.write_bytes(b"ITEM: UNITS\nmetal\n" + frame + frame.replace(b"100", b"200")[:-5])
    preview = base64.b64decode(queue_remote.latest_dump_frame(dump)["data"])
    assert preview == b"ITEM: UNITS\nmetal\n" + frame


async def test_runner_uses_shared_range_reader(tmp_path, monkeypatch):
    trajectory = tmp_path / "live.lammpstrj"
    far_offset = 22 * 1024**3
    with trajectory.open("wb") as stream:
        stream.write(b"first frame\n")
        stream.seek(far_offset)
        stream.write(b"last frame\n")
    monkeypatch.setattr(queue_remote, "job_root", lambda payload: tmp_path)
    ops = RunnerOperations(tmp_path)
    payload = {
        "action": "queue_browse",
        "operation": "range",
        "path": trajectory.name,
        "limit": 12,
    }
    first = await ops.execute("slurm", payload | {"offset": 0})
    assert base64.b64decode(first["data"]) == b"first frame\n"
    assert first["size"] > far_offset
    with trajectory.open("ab") as stream:
        stream.write(b"new frame\n")
    last = await ops.execute(
        "slurm",
        payload
        | {
            "offset": far_offset,
            "snapshot": first["snapshot"],
        },
    )
    assert base64.b64decode(last["data"]) == b"last frame\n"
    assert last["size"] == first["size"]
