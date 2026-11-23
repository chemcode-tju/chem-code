"""Read only facts supported by legacy VASP inputs and captured OUTCAR excerpts."""

import re

from chemistry_agent.automation.slurm_remote import md_number, vasp_convergence

HEAD_BYTES = 128 * 1024
TAIL_BYTES = 2 * 1024 * 1024
TAGS = ("GGA", "METAGGA", "ENCUT", "ISPIN", "NELECT", "LSOL", "LHFCALC", "LDAU", "NSW", "IBRION")
VASP_FILES = ("POSCAR", "CONTCAR", "INCAR", "KPOINTS", "OSZICAR")


def text(block: bytes) -> str:
    return block.decode("utf-8", errors="replace")


def vasp_facts(files: dict[str, bytes], head: bytes, tail: bytes, size: int) -> dict:
    structure = text(files.get("CONTCAR") or files.get("POSCAR", b""))
    incar = text(files.get("INCAR", b""))
    settings = {}
    for source in (re.sub(r"[!#][^\n]*", "", incar), text(head)):
        pairs = re.findall(r"\b(" + "|".join(TAGS) + r")\s*=\s*([^\s;#!]+)", source, re.I)
        settings.update((name.upper(), value) for name, value in pairs)
    lines = structure.splitlines()
    formula = None
    if len(lines) >= 7:
        elements, counts = lines[5].split(), lines[6].split()
        if (
            len(elements) == len(counts)
            and all(re.fullmatch(r"[A-Z][a-z]?", item) for item in elements)
            and all(item.isdecimal() for item in counts)
        ):
            composition: dict[str, int] = {}
            for element, count in zip(elements, counts, strict=True):
                composition[element] = composition.get(element, 0) + int(count)
            formula = "".join(
                item + (str(count) if count != 1 else "")
                for item, count in sorted(composition.items())
            )

    output = text(tail)
    if size > len(tail):
        _, _, output = output.partition("\n")
    # A TOTEN inside the SCF trace also has a separate FREE ENERGIE header.
    # The ionic summary identifies the total in the final configuration.
    matches = list(
        re.finditer(
            r"FREE ENERGIE OF THE ION-ELECTRON SYSTEM[^\n]*\n"
            r"\s*[-]+\s*\n\s*free\s+energy\s+TOTEN\s*=\s*([-+.\dEeDd]+)"
            r"\s+eV[^\n]*\n",
            output,
        )
    )
    energy = None
    iterations = list(re.finditer(r"\bIteration\s+(\d+)\s*\(\s*\d+\s*\)", output))
    last_iteration = iterations[-1].start() if iterations else 0
    complete = "General timing and accounting" in output[last_iteration:]
    if matches:
        last = matches[-1]
        value = md_number(last[1])
        if value is not None and last.start() > last_iteration:
            energy = {"metric": "TOTEN", "value": value, "unit": "eV", "source": "OUTCAR"}

    convergence = vasp_convergence(output, incar, structure, size > len(tail))
    ionic = (
        convergence["ionic_converged"]
        if complete and convergence.get("nsw", 0) > 0 and convergence.get("ibrion") in {1, 2, 3}
        else None
    )
    electronic = None
    if energy is not None and complete and iterations:
        # The last ionic step is located by the last VASP iteration (i, j) group.
        start = iterations[-1].start()
        for iteration in reversed(iterations):
            if iteration[1] != iterations[-1][1]:
                break
            start = iteration.start()
        if "aborting loop because EDIFF is reached" in output[start : matches[-1].start()]:
            electronic = True

    functional = {"PE": "PBE", "PS": "PBEsol", "RE": "revPBE"}.get(settings.get("GGA", ""))
    if settings.get("METAGGA", "--").upper() not in {"--", "NONE", "F", "FALSE"}:
        functional = settings["METAGGA"]
    if settings.get("LHFCALC", "").upper().strip(".") in {"T", "TRUE"}:
        functional = "hybrid"
    return {
        "code": "VASP",
        "version": next(iter(re.findall(r"\bvasp\.([0-9.]+)", text(head), re.I)), None),
        "formula": formula,
        "functional": functional,
        "settings": settings,
        "pseudopotential_labels": list(
            dict.fromkeys(re.findall(r"TITEL\s*=\s*([^\r\n]+)", text(head)))
        ),
        "energy": energy,
        "output_complete": complete if tail else None,
        "electronic_converged": electronic,
        "ionic_converged": ionic,
        "scientifically_validated": False,
        "outcar_bytes": size or None,
    }
