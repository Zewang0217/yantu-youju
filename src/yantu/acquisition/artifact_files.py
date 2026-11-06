"""采集产物共用的稳定序列化与原子替换；不承担发布权限或事务决策。"""

import json
import os
import uuid
from pathlib import Path


def canonical_json(value: dict) -> bytes:
    """稳定摘要载荷；NaN等非标准JSON值必须在写盘前失败。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def atomic_write(path: Path, body: bytes) -> None:
    """先完整写入同目录临时文件，再原子替换；不留下半份目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)
