"""Queue browsing binds file reads to the account's current Slurm job identity."""

import base64
import subprocess
from datetime import datetime, timedelta

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
    (directory / "images" / "frame.dump").write_text("ITEM: TIMESTEP\n0\n")
    (directory / "images" / "frame.data").write_text(
        "LAMMPS data\n1 atoms\n1 atom types\n\nMasses\n\n1 63.546 # Cu\n"
    )
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
        assert [entry["name"] for entry in nested.json()["entries"]] == [
            "frame.data", "frame.dump", "POSCAR"
        ]
        dump = {"path": "images/frame.dump"}
        from_job = client.get(f"{browser}/companions", params=params | dump)
        from_tree = client.get(
            f"{browser}/companions", params={"source": "local", "root": str(directory)} | dump
        )
        assert from_job.is_success and from_job.json() == from_tree.json()
        assert from_tree.json()["companions"][0]["name"] == "frame.data"
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


def test_job_dashboard_aggregates_scheduler_account(settings_factory, monkeypatch):
    settings = settings_factory(automation_enabled=True)
    Environments(settings.platform_path).save(
        Environment(id="local-hpc", name="Local Slurm", transport="local")
    )
    owner = queue_remote.pwd.getpwuid(queue_remote.os.getuid()).pw_name
    today = datetime.now().date()
    now = f"{today}T00:00:00"
    older = today - timedelta(days=30)
    historical = f"{older}T21:00:00"
    recent_end = f"{today - timedelta(days=1)}T09:00:00"
    queue = [
        ("12_3", owner, "batch", "RUNNING", "1:11", now, "/", "relaxation"),
        ("13", owner, "batch", "PENDING", "0:00", now, "/", "md"),
        ("14", owner, "batch", "SUSPENDED", "0:10", now, "/", "suspended"),
    ]
    accounting = [
        ("12_3", owner, "RUNNING", now, "Unknown", "3600", "900", "cpu=4,gres/gpu=1"),
        (
            "10", owner, "COMPLETED", historical, recent_end, "7200", "1800",
            "cpu=4,gres/gpu=2,gres/gpu:a100=2",
        ),
        ("10", owner, "RESIZING", historical, recent_end, "90", "90", "cpu=1,gres/gpu=1"),
        (
            "11", owner, "TIMEOUT", historical, now, "1800", "300",
            "cpu=6,gres/gpu:a100=2,gres/gpu:h100=1",
        ),
        ("15", owner, "CANCELLED by 12", historical, now, "Unknown", "30", ""),
        ("16", "another-account", "COMPLETED", historical, now, "9999", "900", "cpu=2,gres/gpu=4"),
        ("10.batch", owner, "COMPLETED", historical, recent_end, "7200", "1800", "gres/gpu=2"),
        ("17", owner, "COMPLETED", historical, now, "400", "100", "cpu=4,gres/gpu=4"),
        ("18", owner, "COMPLETED", historical, now, "500", "250", "cpu=2,gres/gpu=2"),
        *(
            (str(100 + index), owner, "COMPLETED", historical, historical, "3600", "360", "cpu=10")
            for index in range(310)
        ),
    ]
    unavailable = set()
    accounting_calls = []
    supports_array = True
    allocated_nodes = {"17": "gpu-single", "18": "gpu-mixed"}
    retention = "NONE"
    query_start = queue_remote.board_start(today, "year")

    def scheduler(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        executable = command[0]
        if executable == "scontrol":
            assert command[1:] == ["show", "config"]
            config = "AccountingStorageTRES = cpu,gres/gpu"
            return subprocess.CompletedProcess(command, 0, config, "")
        if executable == "sacctmgr":
            return subprocess.CompletedProcess(command, 0, f"PurgeJobAfter = {retention}", "")
        if executable == "sinfo":
            assert command[1:] == ["-N", "-h", "-o", "%N|%G"]
            nodes = "gpu-single|gpu:a6000:4(S:0-3)\ngpu-mixed|gpu:a100:2,gpu:h100:2\n"
            return subprocess.CompletedProcess(command, 0, nodes, "")
        assert command[command.index("-u") + 1] == owner
        if executable in unavailable:
            raise FileNotFoundError(executable)
        if executable == "sacct":
            expanded = "--array" in command
            accounting_calls.append(expanded)
            if expanded and not supports_array:
                raise subprocess.CalledProcessError(
                    1, command, stderr="sacct: unrecognized option '--array'"
                )
            assert "-X" in command
            assert command[command.index("-S") + 1] == f"{query_start}T00:00:00"
            stdout = "\n".join(
                "|".join((*row, allocated_nodes.get(row[0], ""))) for row in accounting
            )
        else:
            assert executable == "squeue"
            stdout = "\n".join("\x1f".join(row) for row in queue)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(queue_remote.subprocess, "run", scheduler)
    with TestClient(
        create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 1)
    ) as client:
        assert client.get("/monitor/board").status_code == 200
        path = "/api/job-monitor/local-hpc/dashboard"
        result = client.get(path).json()
        days = {row["date"]: row for row in result["days"]}
        assert len(days) == (today - query_start).days + 1
        assert result["period"] == "year"
        assert result["coverage_start"] == str(older)
        assert days[str(query_start)] == {"date": str(query_start), "submitted": None}
        assert result["active"] == {"running": 1, "queued": 1, "other": 1}
        assert result["accounting_available"] and result["gpu_accounting_available"]
        assert days[str(older)]["submitted"] == 315
        assert result["submission_hours"][21] == 315
        assert result["submission_hours"][0] == 3
        assert days[str(today - timedelta(days=1))]["cpu_seconds"] == 7200
        assert days[str(today - timedelta(days=1))]["gpu_seconds"] == 3600
        assert days[str(older)]["missing_gpu"] == 0
        assert days[str(today)] == {
            "date": str(today),
            "submitted": 3,
            "completed": 2,
            "failed": 1,
            "cancelled": 1,
            "cpu_seconds": 2700,
            "missing_cpu": 1,
            "gpu_seconds": 1800,
            "missing_gpu": 1,
        }
        breakdown = {
            (row["model"], row["cards"], row["source"]): (
                row["elapsed_seconds"], row["gpu_seconds"]
            )
            for row in result["gpu_breakdown"]
        }
        assert breakdown == {
            ("a100", 2, "accounting"): (2100, 4200),
            ("h100", 1, "accounting"): (300, 300),
            ("a6000", 4, "node"): (100, 400),
            (None, 2, "unrecorded"): (250, 500),
        }
        assert sum(seconds for _, seconds in breakdown.values()) == 5400
        supports_array = False
        compatibility = client.get(path).json()
        assert accounting_calls == [True, True, False]
        assert compatibility["days"] == result["days"]
        assert compatibility["accounting_available"]
        retained_week = client.get(path, params={"period": "week"}).json()
        assert len(retained_week["days"]) == 7
        assert retained_week["days"][0]["submitted"] == 0
        assert sum(row["submitted"] for row in retained_week["days"]) == 3
        query_start = queue_remote.ACCOUNTING_EPOCH
        all_visible = client.get(path, params={"period": "all"}).json()
        assert all_visible["days"][0]["date"] == str(older)
        assert all_visible["days"][0]["submitted"] == 315
        assert client.get(path, params={"period": "invalid"}).status_code == 422
        retention = "1 months"
        query_start = queue_remote.months_before(today.replace(day=1), 1)
        limited = client.get(path).json()
        assert limited["coverage_start"] == str(query_start)
        assert next(row for row in limited["days"] if row["date"] == str(query_start))[
            "submitted"
        ] == (315 if query_start == older else 0)
        query_start = today
        same_day = client.get(path, params={"period": "today"}).json()
        assert same_day["days"] == [days[str(today)]]
        retention = "NONE"
        query_start = queue_remote.board_start(today, "year")
        unavailable.add("sacct")
        partial = client.get(path).json()
        assert not partial["accounting_available"] and not partial["gpu_accounting_available"]
        assert partial["active"] == result["active"]
        assert not any(partial["submission_hours"])
        assert all(row["submitted"] is None for row in partial["days"])
        unavailable.clear()
        unavailable.add("squeue")
        partial = client.get(path).json()
        assert partial["active"] is None and partial["accounting_available"]
        assert partial["submission_hours"][0] == 1
        assert client.get(path, headers={"Origin": "https://other.example"}).status_code == 403


def test_lammps_discovers_nested_data_and_latest_complete_frame(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "structure data.data").write_text(
        "LAMMPS data\n\n1 atom types\n\nMasses\n\n1 63.546 # Cu\n\nAtoms # atomic\n\n1 1 0 0 0\n"
    )
    (tmp_path / "incorrect.data").write_text("Masses\n\n1 12.011 # C\n")
    (tmp_path / "in.trajectory").write_text(
        'units metal\ninclude inputs/system.in\nread_data "inputs/structure data.data"\n'
        "dump actual all custom 10 trajectory.lammpstrj id type x y z\n"
    )
    (tmp_path / "in.other").write_text(
        "units real\nread_data incorrect.data\n"
        "dump other all custom 10 other.lammpstrj id type x y z\n"
    )
    (tmp_path / "log.other").write_text("units real\nmass 1 12.011\n")
    (inputs / "system.in").write_text("boundary p p p\n")
    parts = {
        item["name"]: item["text"]
        for item in queue_remote.lammps_metadata(tmp_path, "trajectory.lammpstrj")
    }
    assert "63.546 # Cu" in parts["inputs/structure data.data"]
    assert "Atoms # atomic" not in parts["inputs/structure data.data"]
    assert parts["inputs/system.in"] == "boundary p p p"
    assert "incorrect.data" not in parts
    assert "in.other" not in parts and "log.other" not in parts
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
