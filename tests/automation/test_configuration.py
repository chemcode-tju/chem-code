import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from chemistry_agent.automation.configuration import (
    CalculationConfiguration,
    CalculationProtocol,
    PotentialSource,
)
from chemistry_agent.errors import AgentError

POSCAR = "Cu\n1\n3.6 0 0\n0 3.6 0\n0 0 3.6\nCu\n1\nDirect\n0 0 0\n"


@pytest.fixture
def configured(tmp_path, monkeypatch):
    pytest.importorskip("aiida")
    import aiida_vasp.data.potcar as potcar

    from chemistry_agent.automation import backend

    monkeypatch.setenv("AIIDA_PATH", str(tmp_path))
    config = CalculationConfiguration(tmp_path)
    server, code = uuid4(), uuid4()
    config.save(
        "sources",
        PotentialSource(id="pbe", server_id=server, root="/approved/PBE", family="test-pbe"),
    )
    config.save(
        "protocols",
        CalculationProtocol(
            id="static",
            description="Test only",
            server_id=server,
            code_id=code,
            potential_source_id="pbe",
            resources={"queue": "test"},
            incar={"ENCUT": 520, "GGA": "PE"},
            kmesh=(2, 2, 2),
            max_atoms=2,
            allowed_elements=["Cu"],
            default=True,
        ),
    )
    imported, transfers = [], []

    class FakePotential:
        def __init__(self, name):
            self.element = name.split("_")[0]
            self.full_name = name
            self.uuid = str(uuid4())
            self.sha512 = "checksum"
            self.base = SimpleNamespace(attributes={"functional": "PBE"})

        @staticmethod
        def get_or_create_from_file(path):
            assert Path(path).name == "POTCAR"
            assert Path(path).read_text() == "PRIVATE-POTCAR-CONTENT"
            node = FakePotential(Path(path).parent.name)
            imported.append(node)
            return node, True

    nodes = []
    group = SimpleNamespace(nodes=nodes, add_nodes=nodes.append)
    monkeypatch.setattr(potcar, "PotcarData", FakePotential)
    monkeypatch.setattr(
        potcar,
        "PotcarGroup",
        SimpleNamespace(collection=SimpleNamespace(get_or_create=lambda **kwargs: (group, True))),
    )

    class Transport:
        missing = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def isfile(self, path):
            assert path == "/approved/PBE/Cu/POTCAR"
            return not self.missing

        def getfile(self, remote, local):
            transfers.append(remote)
            Path(local).write_text("PRIVATE-POTCAR-CONTENT")

    transport = Transport()
    monkeypatch.setattr(
        backend, "load_server", lambda identifier: SimpleNamespace(get_transport=lambda: transport)
    )
    return backend, config, transport, nodes, transfers


def test_potentials_are_imported_on_demand_once_and_never_returned(configured):
    backend, config, transport, nodes, transfers = configured
    first = backend.resolve_calculation({"structure": {"content": POSCAR}})
    assert first["request"]["potential_mapping"] == {"Cu": "Cu"}
    assert first["potentials"][0]["imported"]
    assert "PRIVATE-POTCAR" not in json.dumps(first)
    second = backend.resolve_calculation(
        {"structure": {"content": POSCAR}, "incar": {"encut": 600}}
    )
    assert second["request"]["incar"]["ENCUT"] == 600
    assert not second["potentials"][0]["imported"]
    assert len(transfers) == len(nodes) == 1


def test_missing_or_wrong_functional_fails_without_preview(configured):
    backend, config, transport, nodes, transfers = configured
    transport.missing = True
    with pytest.raises(AgentError) as error:
        backend.resolve_calculation({"structure": {"content": POSCAR}})
    assert error.value.code == "potential_missing"
    assert not nodes and not transfers
    with pytest.raises(AgentError) as error:
        backend.resolve_calculation({"structure": {"content": POSCAR}, "incar": {"GGA": "PS"}})
    assert error.value.code == "functional_mismatch"


def test_scope_and_adaptive_kmesh(configured):
    backend, config, transport, nodes, transfers = configured
    with pytest.raises(AgentError) as error:
        backend.resolve_calculation({"structure": {"content": POSCAR.replace("Cu", "Fe")}})
    assert error.value.code == "protocol_scope_mismatch"
    assert not transfers
    protocol = CalculationProtocol.model_validate(config.get("protocols", "static"))
    protocol.kpoint_spacing = 0.25
    config.save("protocols", protocol)
    result = backend.resolve_calculation({"structure": {"content": POSCAR}})
    assert result["request"]["kmesh"] == [7, 7, 7]
    explicit = backend.resolve_calculation({"structure": {"content": POSCAR}, "kmesh": [1, 2, 3]})
    assert explicit["request"]["kmesh"] == [1, 2, 3]


def test_cached_non_pbe_is_rejected(configured):
    backend, config, transport, nodes, transfers = configured
    backend.resolve_calculation({"structure": {"content": POSCAR}})
    nodes[0].base.attributes["functional"] = "LDA"
    with pytest.raises(AgentError) as error:
        backend.resolve_calculation({"structure": {"content": POSCAR}})
    assert error.value.code == "potential_functional_mismatch"


@pytest.mark.parametrize("root", ["/approved/../private", "/archive/PBE", "relative"])
def test_source_path_validation(root):
    with pytest.raises(ValidationError):
        PotentialSource(id="pbe", server_id=uuid4(), family="PBE", root=root)


def test_target_science_only_at_preview_and_snapshot_is_reusable(configured):
    from chemistry_agent.automation.configuration import ExecutionTarget

    backend, config, _, _, transfers = configured
    legacy = config.get("protocols", "static")
    target = ExecutionTarget(
        id="cluster-low",
        default=True,
        resources={"queue": "low"},
        **{key: legacy[key] for key in ("code_id", "server_id", "potential_source_id")},
    )
    config.save("execution_targets", target)
    with pytest.raises(AgentError) as error:
        backend.resolve_calculation({"structure": {"content": POSCAR}})
    assert error.value.code == "science_required" and not transfers
    settings = {"incar": {"ENCUT": 300}, "kmesh": [3, 3, 1]}
    result = backend.resolve_calculation({"structure": {"content": POSCAR}, **settings})
    assert result["request"]["resources"]["queue"] == "low"
    assert result["request"]["incar"] == settings["incar"]
    assert result["request"]["kmesh"] == settings["kmesh"]
    snapshot = config.resolve(None, **settings).model_dump(mode="json")
    target.resources.queue = "batch"
    config.save("execution_targets", target)
    restored = backend.resolve_calculation(
        {"structure": {"content": POSCAR}, "protocol_snapshot": snapshot}
    )
    assert restored["request"]["resources"]["queue"] == "low"
    assert (
        backend.resolve_calculation({"structure": {"content": POSCAR}, "protocol_id": "static"})[
            "request"
        ]["incar"]["ENCUT"]
        == 520
    )
