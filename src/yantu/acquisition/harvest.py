"""采集门面(R3):一次调用完成 抓取→分发解析→快照落盘→入口库登记。

未来消费方:首批其余4校接入、LLM 采集Agent(06 设计稿的采集工作流)。
失败单一出口:抓取失败登记 fetch_failed 并返回 outcome.ok=False,
不在此重试(重试/退避属 07 契约调用方);fetch_fn 可注入以便离线测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .fetcher import FetchError, Snapshot, fetch
from .parsers import ParseResult, ParserRegistry, DEFAULT_REGISTRY, parse_snapshot
from .store import EntryBook, EntryRecord, SnapshotStore


@dataclass
class HarvestOutcome:
    """collect 的结果。ok=True 只表示**网络层成功**(抓到快照并落盘);

    解析是否降级要看 parse_result.status(UNSUPPORTED/PARTIAL 仍 ok=True)。
    """

    url: str
    name: str
    ok: bool
    snapshot: Optional[Snapshot] = None
    parse_result: Optional[ParseResult] = None
    snapshot_path: Optional[Path] = None
    error: str = ""

    @property
    def sha256(self) -> str:
        return self.snapshot.sha256 if self.snapshot else ""


class HarvestService:
    """fetch_fn 契约:成功返回 Snapshot;失败抛 FetchError(其他异常会被
    兜底登记为 fetch_failed 后原样上抛,不吞编程错误)。"""

    def __init__(
        self,
        snapshot_store: SnapshotStore,
        entry_book: EntryBook,
        registry: ParserRegistry = DEFAULT_REGISTRY,
        fetch_fn: Callable[[str], Snapshot] = fetch,
    ) -> None:
        self._store = snapshot_store
        self._book = entry_book
        self._registry = registry
        self._fetch = fetch_fn

    def _mark_failed(self, url: str, name: str, school: str, department: str, reason: str) -> None:
        """失败登记:继承旧记录字段(哈希/最近成功时间等入口资产不丢,G8),
        只更新 last_status/notes;登记后落盘。"""
        old = self._book.get(url) or EntryRecord(url=url)
        old.name = name or old.name
        old.school = school or old.school
        old.department = department or old.department
        old.last_status = "fetch_failed"
        old.notes = reason[:120]
        self._book.upsert(old)
        self._book.save()

    def collect(
        self,
        url: str,
        name: str = "",
        school: str = "",
        department: str = "",
    ) -> HarvestOutcome:
        try:
            snap = self._fetch(url)
        except FetchError as e:
            self._mark_failed(url, name, school, department, e.reason)
            return HarvestOutcome(url=url, name=name, ok=False, error=str(e))
        except Exception as e:  # 非预期异常:登记后上抛,不吞
            self._mark_failed(url, name, school, department, f"{type(e).__name__}: {e}")
            raise

        parsed = parse_snapshot(snap, self._registry)
        path = self._store.save(snap, title=parsed.title)
        old = self._book.get(url) or EntryRecord(url=url)
        self._book.upsert(
            EntryRecord(
                url=url,
                name=name or old.name,
                school=school or old.school,
                department=department or old.department,
                carrier=snap.carrier,
                last_status="ok",
                last_sha256=snap.sha256,
                last_fetched=snap.fetched_at.isoformat(timespec="seconds"),
                notes=old.notes,
            )
        )
        self._book.save()
        return HarvestOutcome(
            url=url, name=name, ok=True, snapshot=snap,
            parse_result=parsed, snapshot_path=path,
        )
