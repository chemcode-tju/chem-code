"""A tiny real CalcJob to verify structure upload, scheduling and retrieval."""

import hashlib
import io

from aiida import orm
from aiida.common import CalcInfo, CodeInfo
from aiida.engine import CalcJob, CalcJobProcessSpec, ExitCode
from aiida.parsers import Parser
from ase.io import write


def poscar_text(structure: orm.StructureData) -> str:
    output = io.StringIO()
    write(output, structure.get_ase(), format="vasp", direct=True, vasp5=True)
    return output.getvalue()


class StructureProbeCalculation(CalcJob):
    @classmethod
    def define(cls, spec: CalcJobProcessSpec) -> None:
        super().define(spec)
        spec.input("structure", valid_type=orm.StructureData)
        spec.input("metadata.options.parser_name", default="chemistry.structure_probe")
        spec.input("metadata.options.withmpi", default=False)
        spec.output("report", valid_type=orm.Dict)
        spec.exit_code(
            310, "ERROR_INVALID_REPORT", message="Missing or inconsistent structure probe output."
        )

    def prepare_for_submission(self, folder) -> CalcInfo:
        content = poscar_text(self.inputs.structure)
        with folder.open("POSCAR", "w") as stream:
            stream.write(content)
        with folder.open("probe.sh", "w") as stream:
            stream.write("set -eu\nhostname\nsha256sum POSCAR\nprintf 'CHEM_AGENT_PROBE_OK\\n'\n")
        code = CodeInfo()
        code.code_uuid = self.inputs.code.uuid
        code.cmdline_params = ["probe.sh"]
        code.stdout_name = "probe.out"
        code.stderr_name = "probe.err"
        code.withmpi = False
        info = CalcInfo()
        info.codes_info = [code]
        info.retrieve_list = ["probe.out", "probe.err", "POSCAR"]
        return info


class StructureProbeParser(Parser):
    def parse(self, **kwargs) -> ExitCode | None:
        try:
            report = self.retrieved.base.repository.get_object_content("probe.out")
            content = self.retrieved.base.repository.get_object_content("POSCAR")
            expected = hashlib.sha256(poscar_text(self.node.inputs.structure).encode()).hexdigest()
            if (
                "CHEM_AGENT_PROBE_OK" not in report
                or expected not in report
                or hashlib.sha256(content.encode()).hexdigest() != expected
            ):
                return self.exit_codes.ERROR_INVALID_REPORT
        except (OSError, KeyError):
            return self.exit_codes.ERROR_INVALID_REPORT
        self.out(
            "report",
            orm.Dict(
                dict={
                    "structure_sha256": expected,
                    "atom_count": len(self.node.inputs.structure.sites),
                    "remote_output": report,
                    "purpose": "transport_and_scheduler_probe",
                    "scientific_calculation": False,
                }
            ),
        )
        return None
