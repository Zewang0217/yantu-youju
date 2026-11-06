"""单写者文件发布库：不可变审核记录先落盘，再原子切换当前版本指针。

草案隔离记录不更新推荐读取指针。进程互斥锁冲突时显式失败，不自动抢占；
跨主机租约及数据库事务属于后续持久化适配器。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.models import Opportunity
from .store import SnapshotStore, SnapshotStorageError, record_to_opportunity
from .artifact_files import atomic_write, canonical_json


class PublicationError(RuntimeError):
    """发布冲突、存储损坏或审计写入失败，禁止继续发布。"""


def publication_version(record: dict) -> str:
    """事实内容寻址排除请求审计字段；迁移清单需另对完整记录计算摘要。"""
    return hashlib.sha256(canonical_json({k: v for k, v in record.items() if k != 'requestId'})).hexdigest()


def validate_current_record(record: dict, oid: str, snapshots: SnapshotStore, *,
                            snapshot_error: str = '当前事实的源快照不可验证，禁止用于推荐') -> None:
    """校验当前记录可供推荐及其来源快照；由调用者持有自己的读事务。"""
    if record.get('status') not in ('published', 'published_partial') or record.get('oid') != oid:
        raise PublicationError('当前指针引用了不可推荐记录')
    try:
        snapshots.read(record['source']['sha256'])
    except (SnapshotStorageError, KeyError, TypeError) as error:
        raise PublicationError(snapshot_error) from error


class PublicationRepository:
    """以oid区分当前事实；版本记录包含审计与完整可回溯机会对象。"""

    def __init__(self, root: Path, snapshots: SnapshotStore):
        self.root = Path(root)
        self.snapshots = snapshots

    @contextmanager
    def _writer(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / "writer.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise PublicationError("发布库已有写入者；确认任务状态后再恢复") from e
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink()

    def _pointer(self, oid: str) -> Path:
        key = hashlib.sha256(oid.encode("utf-8")).hexdigest()
        return self.root / "current" / f"{key}.json"

    _atomic_write = staticmethod(atomic_write)

    def current_version(self, oid: str) -> str | None:
        """返回已提交指针；无已发布版本时为None。"""
        path = self._pointer(oid)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError) as e:
            raise PublicationError("当前版本指针不可读") from e

    def write(self, record: dict, *, expected_version: str | None) -> str:
        """原子提交已审核记录，或仅保留隔离记录；旧expected_version拒绝覆盖。

        相同内容重投直接幂等返回；requestId属于首次发布审计，不改变事实版本。
        """
        version = publication_version(record)
        target = self.root / "versions" / f"{version}.json"
        try:
            with self._writer():
                current = self.current_version(record["oid"])
                if current != version and current != expected_version:
                    raise PublicationError("当前事实版本已变更，必须重新核验后发布")
                if current and record["status"] in ("published", "published_partial"):
                    prior = self.read_record(current)
                    if datetime.fromisoformat(record["source"]["fetchedAt"]) < datetime.fromisoformat(prior["source"]["fetchedAt"]):
                        raise PublicationError("较早获取的快照不能替换较新来源；回放请使用独立资料库")
                if not target.exists():
                    self._atomic_write(target, canonical_json(record))
                else:
                    self.read_record(version)
                if record["status"] in ("published", "published_partial") and current != version:
                    self._atomic_write(self._pointer(record["oid"]), canonical_json({"version": version}))
        except OSError as e:
            raise PublicationError("发布/审计持久化失败，未确认提交") from e
        return version

    def read_record(self, version: str) -> dict:
        """校验版本摘要后读取审核记录；不接受任意文件路径。"""
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise PublicationError("非法版本引用")
        try:
            value = json.loads((self.root / "versions" / f"{version}.json").read_text(encoding="utf-8"))
            actual = publication_version(value)
        except (OSError, ValueError) as e:
            raise PublicationError("事实版本记录不可读") from e
        if actual != version:
            raise PublicationError("事实版本摘要不一致")
        return value

    def load(self, oid: str) -> Opportunity | None:
        """推荐侧只读当前已发布版本；隔离记录永不作为候选事实返回。"""
        version = self.current_version(oid)
        if version is None:
            return None
        record = self.read_record(version)
        validate_current_record(record, oid, self.snapshots)
        return record_to_opportunity(record["opportunity"])
