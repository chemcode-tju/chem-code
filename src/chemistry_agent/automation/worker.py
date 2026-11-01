"""One JSON request on stdin, one JSON response on stdout; logs go to stderr."""

import contextlib
import json
import sys
import traceback

from chemistry_agent.errors import AgentError


def dispatch(message: dict) -> dict:
    try:
        from aiida import load_profile
        from aiida.common.exceptions import MissingConfigurationError, ProfileConfigurationError
    except ImportError as exc:
        raise AgentError("aiida_missing", "未安装 AiiDA 自动化依赖。", 503) from exc
    try:
        load_profile(message["profile"])
    except (MissingConfigurationError, ProfileConfigurationError) as exc:
        raise AgentError(
            "aiida_profile_missing", "请先运行 scripts/setup_automation.py 初始化独立配置。", 503
        ) from exc
    from chemistry_agent.automation import backend
    from chemistry_agent.automation.branch_retry import preview_retry
    from chemistry_agent.automation.step_data import step_data
    from chemistry_agent.automation.surface import job_structure
    from chemistry_agent.automation.task_files import stage_files, task_files

    actions = {
        "plan_identity": backend.plan_identity,
        "task_files": task_files,
        "stage_files": stage_files,
        "step_data": step_data,
        "branch_retry": preview_retry,
        "status": lambda _: backend.status(),
        "create_server": backend.create_server,
        "test_server": backend.test_server,
        "create_code": backend.create_code,
        "preview": backend.preview,
        "submit": backend.submit_preview,
        "jobs": lambda _: backend.jobs(),
        "validate_structure": backend.validate_structure,
        "catalog": lambda _: backend.catalog(),
        "job_details": backend.job_details,
        "job_structure": job_structure,
        "session_job": backend.session_job,
        "save_configuration": backend.save_configuration,
        "resolve_calculation": backend.resolve_calculation,
        "tracked_jobs": backend.tracked_jobs,
    }
    return actions[message["action"]](message.get("payload", {}))


def main() -> None:
    try:
        message = json.load(sys.stdin)
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(message)
    except AgentError as exc:
        result = {
            "error": {"code": exc.code, "message": exc.message, "status_code": exc.status_code}
        }
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result = {
            "error": {
                "code": "aiida_operation_failed",
                "message": "AiiDA 操作失败，请检查服务日志及输入配置。",
                "status_code": 422,
            }
        }
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
