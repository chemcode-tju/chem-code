import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import Crippen, Descriptors, Lipinski, rdFingerprintGenerator, rdMolDescriptors

from chemistry_agent.errors import AgentError
from chemistry_agent.platform.contracts import OperationContext as ToolContext
from chemistry_agent.provider import ToolCall
from chemistry_agent.schemas import ToolExecution


class MoleculeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    smiles: str = Field(min_length=1, max_length=2000, description="Molecule encoded as SMILES")


class SimilarityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    smiles_a: str = Field(min_length=1, max_length=2000)
    smiles_b: str = Field(min_length=1, max_length=2000)


def parse_molecule(smiles: str):
    mol = Chem.MolFromSmiles(smiles)
    if mol is None or mol.GetNumAtoms() == 0:
        raise ValueError("无法解析 SMILES，请提供有效的分子结构。")
    return mol


def molecular_properties(args: MoleculeInput) -> dict[str, Any]:
    mol = parse_molecule(args.smiles)
    return {
        "canonical_smiles": Chem.MolToSmiles(mol),
        "formula": rdMolDescriptors.CalcMolFormula(mol),
        "molecular_weight_g_mol": round(Descriptors.MolWt(mol), 4),
        "exact_mass_da": round(Descriptors.ExactMolWt(mol), 6),
        "logp_rdkit_crippen": round(Crippen.MolLogP(mol), 4),
        "tpsa_angstrom_squared": round(rdMolDescriptors.CalcTPSA(mol), 4),
        "hydrogen_bond_donors": Lipinski.NumHDonors(mol),
        "hydrogen_bond_acceptors": Lipinski.NumHAcceptors(mol),
        "rotatable_bonds": Lipinski.NumRotatableBonds(mol),
        "ring_count": rdMolDescriptors.CalcNumRings(mol),
        "heavy_atom_count": mol.GetNumHeavyAtoms(),
        "formal_charge": Chem.GetFormalCharge(mol),
        "source": (
            f"RDKit {rdBase.rdkitVersion}; computed descriptors, not experimental measurements"
        ),
    }


def molecular_similarity(args: SimilarityInput) -> dict[str, Any]:
    mol_a, mol_b = parse_molecule(args.smiles_a), parse_molecule(args.smiles_b)
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    score = DataStructs.TanimotoSimilarity(
        generator.GetFingerprint(mol_a), generator.GetFingerprint(mol_b)
    )
    return {
        "canonical_smiles_a": Chem.MolToSmiles(mol_a),
        "canonical_smiles_b": Chem.MolToSmiles(mol_b),
        "tanimoto_similarity": round(score, 6),
        "method": "Morgan bit fingerprint; radius=2; size=2048; includeChirality=false",
        "source": f"RDKit {rdBase.rdkitVersion}",
    }


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable
    contextual: bool = False

    def schema(self) -> dict[str, Any]:
        def compact(schema: Any) -> Any:
            if isinstance(schema, list):
                return [compact(item) for item in schema]
            if not isinstance(schema, dict):
                return schema
            return {
                key: {name: compact(value) for name, value in item.items()}
                if key in {"properties", "$defs"}
                else compact(item)
                for key, item in schema.items()
                if key != "title"
            }

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": compact(self.input_model.model_json_schema()),
            },
        }


class ToolRegistry:
    def __init__(self, extra_tools: list[Tool] | None = None):
        tools = [
            Tool(
                "molecular_properties",
                "Compute molecular formula, mass, and descriptors from "
                "a known SMILES using RDKit. Results are computed, not measured.",
                MoleculeInput,
                molecular_properties,
            ),
            Tool(
                "molecular_similarity",
                "Compare two known SMILES using Morgan fingerprint "
                "Tanimoto similarity. This is structural similarity, not a biological prediction.",
                SimilarityInput,
                molecular_similarity,
            ),
        ]
        self.tools = {tool.name: tool for tool in [*tools, *(extra_tools or [])]}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, call: ToolCall, context: ToolContext | None = None) -> ToolExecution:
        arguments: Any = call.function.arguments
        try:
            # Some providers return literal newlines inside a tool string (e.g. a
            # Python heredoc). Preserve those bytes instead of rejecting valid code.
            arguments = json.loads(arguments, strict=False)
            tool = self.tools.get(call.function.name)
            if tool is None:
                raise ValueError("未知工具，请选择已注册的工具。")
            args = tool.input_model.model_validate(arguments)
            if tool.contextual and context is None:
                raise AgentError("chat_context_required", "请在对话中使用此工具。", 422)
            values = (args, context) if tool.contextual else (args,)
            if inspect.iscoroutinefunction(tool.handler):
                result = await tool.handler(*values)
            else:
                result = await asyncio.to_thread(tool.handler, *values)
            success = True
        except AgentError as exc:
            result = {"error": exc.code, "message": exc.message}
            success = False
        except ValidationError as exc:
            result = {
                "error": "invalid_tool_input",
                "message": "参数校验失败；根据字段位置修改后重试。",
                "fields": [
                    {"field": ".".join(map(str, issue["loc"])), "reason": issue["msg"]}
                    for issue in exc.errors(include_input=False, include_context=False)[:12]
                ],
            }
            success = False
        except json.JSONDecodeError as exc:
            result = {
                "error": "invalid_tool_input",
                "message": f"工具参数 JSON 语法错误：{exc.msg}"
                f"（行 {exc.lineno}，列 {exc.colno}）。",
            }
            success = False
        except ValueError:
            result = {
                "error": "invalid_tool_input",
                "message": "工具名、参数或 SMILES 无效，请根据工具定义修正后重试。",
            }
            success = False
        except Exception:
            result = {"error": "tool_execution_failed", "message": "工具执行失败，请调整任务。"}
            success = False
        return ToolExecution(
            call_id=call.id,
            name=call.function.name,
            arguments=arguments,
            success=success,
            result=result,
        )
