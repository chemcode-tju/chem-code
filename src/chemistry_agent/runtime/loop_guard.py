"""Detect repeated identical observations without restricting tools or job parallelism."""

import hashlib
import json
from dataclasses import dataclass, field

from chemistry_agent.errors import AgentError
from chemistry_agent.schemas import ToolExecution


@dataclass
class ObservationWatch:
    fingerprints: list[str] = field(default_factory=list)
    repeated_rounds: int = 0

    def observe(self, calls: list[ToolExecution]) -> None:
        current = [
            hashlib.sha256(
                json.dumps(
                    [call.name, call.arguments, call.success, call.result],
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
            for call in calls
        ]
        repeated = bool(current) and all(value in self.fingerprints for value in current)
        self.repeated_rounds = self.repeated_rounds + 1 if repeated else 0
        self.fingerprints = list(dict.fromkeys([*current, *self.fingerprints]))[:256]

    def check(self) -> None:
        if self.repeated_rounds >= 8:
            raise AgentError(
                "agent_no_progress",
                "连续八轮重复相同工具输入与结果，已停止本轮推理并保留检查点。"
                "请检查已有结果和下一步条件后再继续；远端作业未取消。",
                409,
            )

    def snapshot(self) -> dict:
        return {"fingerprints": self.fingerprints, "repeated_rounds": self.repeated_rounds}
