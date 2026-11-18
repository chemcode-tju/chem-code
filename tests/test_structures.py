from collections.abc import Callable
from io import StringIO

import httpx
import numpy as np
import pytest
from ase import Atoms
from ase.io import read, write
from fastapi.testclient import TestClient

from chemistry_agent.api import create_app
from chemistry_agent.config import Settings
from chemistry_agent.errors import AgentError
from chemistry_agent.structures import convert_structure


def dump_frame(coordinates: str = "x y z", values: str = "6 7 8", step: int = 0) -> str:
    return (
        f"ITEM: TIMESTEP\n{step}\nITEM: NUMBER OF ATOMS\n2\n"
        "ITEM: BOX BOUNDS pp pp ff\n5 15\n6 26\n7 37\n"
        f"ITEM: ATOMS id type {coordinates}\n2 2 {values}\n1 1 {values}\n"
    )


def parsed(content: str, filename: str, **kwargs: str) -> list[Atoms]:
    converted, count, _ = convert_structure(content.encode(), filename, **kwargs)
    frames = read(StringIO(converted), format="extxyz", index=":")
    assert len(frames) == count
    return frames


def test_dump_mapping_ids_origin_and_multiframe() -> None:
    frames = parsed(dump_frame() + dump_frame(step=1), "water.dump", type_map="Cu O")
    assert len(frames) == 2
    for atoms in frames:
        assert atoms.get_chemical_symbols() == ["Cu", "O"]
        np.testing.assert_allclose(atoms.positions, [[1, 1, 1], [1, 1, 1]])
        np.testing.assert_allclose(atoms.cell, np.diag([10, 20, 30]))
        assert atoms.pbc.tolist() == [True, True, False]
        assert atoms.arrays["id"].tolist() == [1, 2]


def test_scaled_dump_does_not_subtract_origin_twice() -> None:
    atoms = parsed(dump_frame("xs ys zs", "0.1 0.2 0.3"), "x.dump", type_map="H O")[0]
    np.testing.assert_allclose(atoms.positions, [[1, 4, 9], [1, 4, 9]])


def test_dump_triclinic_box_and_explicit_elements() -> None:
    source = (
        "ITEM: TIMESTEP\n0\nITEM: NUMBER OF ATOMS\n1\n"
        "ITEM: BOX BOUNDS xy xz yz pp pp pp\n5 18 2\n6 29 1\n7 37 3\n"
        "ITEM: ATOMS id element xs ys zs\n4 Cu 0.1 0.2 0.3\n"
    )
    atoms = parsed(source, "dump.any")[0]
    np.testing.assert_allclose(atoms.cell, [[10, 0, 0], [2, 20, 0], [1, 3, 30]])
    np.testing.assert_allclose(atoms.positions, [[1.7, 4.9, 9]])
    assert atoms.arrays["id"].tolist() == [4]


@pytest.mark.parametrize("type_map", ["", "Cu", "Cu Bad", "X O"])
def test_dump_rejects_missing_or_incomplete_mapping(type_map: str) -> None:
    with pytest.raises(AgentError, match="结构读取失败"):
        parsed(dump_frame(), "x.dump", type_map=type_map)


def test_gro_units_box_and_concatenated_frames() -> None:
    atoms = Atoms("OH2", positions=[[1, 2, 3], [2, 2, 3], [1, 3, 3]], cell=[10, 20, 30], pbc=True)
    stream = StringIO()
    write(stream, atoms, format="gromacs")
    frames = parsed(stream.getvalue() * 2, "water.gro")
    assert len(frames) == 2
    np.testing.assert_allclose(frames[1].positions, atoms.positions)
    np.testing.assert_allclose(frames[1].cell, atoms.cell)
    assert frames[1].get_chemical_symbols() == atoms.get_chemical_symbols()


def test_data_mapping_and_nonzero_origin() -> None:
    content = (
        "LAMMPS data\n\n2 atoms\n2 atom types\n\n"
        "5 15 xlo xhi\n6 26 ylo yhi\n7 37 zlo zhi\n\n"
        "Atoms # atomic\n\n2 2 6 7 8\n1 1 5 6 7\n"
    )
    atoms = parsed(content, "sample.data", type_map="Cu O")[0]
    assert atoms.get_chemical_symbols() == ["Cu", "O"]
    np.testing.assert_allclose(atoms.positions, [[0, 0, 0], [1, 1, 1]], atol=1e-12)
    assert atoms.arrays["id"].tolist() == [1, 2]


@pytest.mark.parametrize(
    "filename, file_format",
    [
        ("POSCAR", "vasp"),
        ("CONTCAR", "vasp"),
        ("XDATCAR", "vasp-xdatcar"),
        ("water.xyz", "extxyz"),
        ("water.extxyz", "extxyz"),
        ("water.pdb", "proteindatabank"),
    ],
)
def test_standard_format_coordinates(filename: str, file_format: str) -> None:
    atoms = Atoms("OH2", positions=[[1, 2, 3], [2, 2, 3], [1, 3, 3]], cell=[10, 20, 30], pbc=True)
    stream = StringIO()
    write(stream, atoms, format=file_format)
    actual = parsed(stream.getvalue(), filename)[0]
    np.testing.assert_allclose(actual.positions, atoms.positions, atol=1e-6)
    np.testing.assert_allclose(actual.cell, atoms.cell, atol=1e-6)


@pytest.mark.parametrize(
    "content, name",
    [
        (b"", "empty.xyz"),
        (b"garbage", "wrong.gro"),
        (b"\xff", "binary.xyz"),
        (b"1\n\nH nan 0 0\n", "nan.xyz"),
        (b"x", "traj.xtc"),
        (dump_frame().rsplit("\n", 2)[0].encode(), "broken.dump"),
    ],
)
def test_malformed_inputs_return_domain_errors(content: bytes, name: str) -> None:
    with pytest.raises(AgentError) as error:
        convert_structure(content, name, type_map="Cu O")
    assert error.value.status_code == 422


def test_structure_routes_and_limits(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(
        settings_factory(), transport=httpx.MockTransport(lambda _: httpx.Response(200))
    )
    with TestClient(app) as client:
        assert client.get("/structures").status_code == 200
        assert 'data-src="/structures?embed=1"' in client.get("/").text
        for name in ("app.js", "import.js", "trajectory-store.js", "vendor/3Dmol-min.js"):
            assert client.get(f"/assets/structure/{name}").status_code == 200
        renderer = client.get("/assets/structure/vendor/3Dmol-min.js")
        assert renderer.is_success
        assert "vec3(0.34) + 0.62*vLight" in renderer.text
        assert "vec3(0.34) + 0.62*light" in renderer.text
        assert "if(this.isWebGL1()||this._fb)return;" in renderer.text
        response = client.post(
            "/structures/convert",
            params={"filename": "sample.dump", "type_map": "Cu O"},
            content=dump_frame(),
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 200
        assert response.headers["x-structure-frames"] == "1"
        assert response.headers["cache-control"] == "no-store"
        assert (
            client.post("/structures/convert?filename=x.dump", content=dump_frame()).status_code
            == 422
        )
        assert (
            client.post("/structures/convert?filename=x&format=pickle", content=b"x").status_code
            == 422
        )
        monkeypatch.setattr("chemistry_agent.structures.MAX_UPLOAD_BYTES", 4)
        assert (
            client.post("/structures/convert?filename=x.xyz", content=b"12345").status_code == 413
        )


def test_structure_conversion_requires_login(settings_factory: Callable[..., Settings]) -> None:
    app = create_app(settings_factory(auth_enabled=True))
    with TestClient(app) as client:
        assert client.get("/structures").status_code == 401
        assert client.post("/structures/convert?filename=x.xyz", content=b"x").status_code == 401


def test_cif_attachment_converts_cell_and_fractional_coordinates() -> None:
    text = """data_slab
_cell_length_a 4
_cell_length_b 5
_cell_length_c 20
_cell_angle_alpha 90
_cell_angle_beta 90
_cell_angle_gamma 90
loop_
_atom_site_label
_atom_site_type_symbol
_atom_site_fract_x
_atom_site_fract_y
_atom_site_fract_z
Cu1 Cu 0.25 0.5 0.75
"""
    atoms = parsed(text, "slab.cif")[0]
    assert atoms.get_chemical_symbols() == ["Cu"]
    np.testing.assert_allclose(atoms.positions, [[1, 2.5, 15]])
