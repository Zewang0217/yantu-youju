"""应用错误的安全结构事件：保留类型/栈帧，不记录异常消息或链中的来源原文。"""

import json
import logging
import traceback


def log_failure(logger: logging.Logger, error: BaseException, *, event: str, request_id: str | None = None, source_id: str | None = None,
                run_id: str | None = None, task_id: str | None = None, step_id: str | None = None, attempt: int | None = None) -> None:
    """仅接受应用生成的关联ID；保留异常链类型和帧位置，不展开locals/message。"""
    failures, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(failures) < 8:
        seen.add(id(current))
        frames = traceback.extract_tb(current.__traceback__)
        failures.append({"type": type(current).__name__, "frames": [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name} for frame in frames]})
        current = current.__cause__ or (current.__context__ if not current.__suppress_context__ else None)
    record = {"event": event, "requestId": request_id, "sourceId": source_id, "failures": failures, "exceptionMessages": "omitted"}
    record.update({key: value for key, value in (("runId", run_id), ("taskId", task_id), ("stepId", step_id), ("attempt", attempt)) if value is not None})
    logger.error(json.dumps(record, ensure_ascii=False))
