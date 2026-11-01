"""Distinguish Hessian evaluation from a force-based geometry optimization."""


def vibrational_incar(incar: dict) -> bool:
    value = {k.upper(): v for k, v in incar.items()}.get("IBRION")
    return str(value).strip() in {"5", "6", "7", "8"}


def output_settings(incar: dict) -> dict:
    settings = {"ADDITIONAL_RETRIEVE_LIST": ["OSZICAR", "XDATCAR"]}
    if vibrational_incar(incar):
        # The plugin's default force-convergence gate applies to relaxations. A
        # phonon job's success is instead established by its actual frequency data.
        settings["ADDITIONAL_RETRIEVE_LIST"].append("POSCAR")
        settings["parser_settings"] = {"check_ionic_convergence": False}
    return settings
