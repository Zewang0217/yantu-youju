"""落盘存储(F7 Repository):快照库、入口库、机会库。

设计要点:
- 快照按内容寻址(sha256 文件名)——同 URL 变更产生新文件而非覆盖,
  支撑"同URL字段级修订"的证据链(核验G8/INV-2);
- 入口库(EntryBook)即"可跨年复用的入口"资产,记录最近抓取状态;
- 机会库以 codec spec 形态落盘(07 数据契约:规则可版本化/diff)。
实现为 JSON/文件,接口稳定后可在 M5 换平台存储而不动内核。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..domain import condition_codec as codec
from ..domain.models import Evidence, Opportunity, Window


@dataclass
class EntryRecord:
    """来源入口(入口库一条记录)。"""

    url: str
    name: str = ""
    school: str = ""
    department: str = ""
    carrier: str = ""
    last_status: str = ""  # ok / fetch_failed
    last_sha256: str = ""
    last_fetched: str = ""  # ISO 时间
    notes: str = ""


class EntryBook:
    """入口库:JSON 文件承载,url 为主键;upsert 即更新最近状态。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._entries: dict[str, EntryRecord] = {}
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = {e["url"]: EntryRecord(**e) for e in raw.get("entries", [])}

    def upsert(self, record: EntryRecord) -> None:
        self._entries[record.url] = record

    def get(self, url: str) -> Optional[EntryRecord]:
        return self._entries.get(url)

    def all(self) -> list[EntryRecord]:
        return list(self._entries.values())

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [asdict(e) for e in self._entries.values()]}
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class SnapshotStore:
    """快照库:内容寻址 <sha256>.html + 同名 .meta.json;不覆盖历史。"""

    def __init__(self, root: Path):
        self._root = Path(root)

    def save(self, snapshot, title: str = "") -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        body_path = self._root / f"{snapshot.sha256}.html"
        if not body_path.exists():  # 内容寻址:已存在即同一内容
            body_path.write_bytes(snapshot.body)
        meta_path = self._root / f"{snapshot.sha256}.meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "url": snapshot.url,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "status_code": snapshot.status_code,
                    "content_type": snapshot.content_type,
                    "sha256": snapshot.sha256,
                    "title": title,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return body_path

    def path_of(self, sha256: str) -> Path:
        return self._root / f"{sha256}.html"


# —— 机会记录与领域对象互转(demo 与未来采集Agent共用) ——

def opportunity_to_record(op: Opportunity) -> dict:
    return {
        "oid": op.oid,
        "school": op.school,
        "department": op.department,
        "program": op.program,
        "batch": op.batch,
        "entry_year": op.entry_year,
        "degree_type": op.degree_type,
        "windows": [
            {
                "entry_name": w.entry_name,
                "required": w.required,
                "start": w.start.isoformat() if w.start else None,
                "end": w.end.isoformat() if w.end else None,
            }
            for w in op.windows
        ],
        "conditions_spec": codec.spec(op.conditions) if op.conditions else None,
        "priorities_spec": [codec.spec_priority(p) for p in op.priorities],
        "evidence": [
            {
                **asdict(e),
                "version_time": e.version_time.isoformat() if e.version_time else None,
                "verified_at": e.verified_at.isoformat() if e.verified_at else None,
            }
            for e in op.evidence
        ],
    }


def record_to_opportunity(rec: dict) -> Opportunity:
    return Opportunity(
        oid=rec["oid"],
        school=rec["school"],
        department=rec["department"],
        program=rec["program"],
        batch=rec["batch"],
        entry_year=rec["entry_year"],
        degree_type=rec["degree_type"],
        windows=tuple(
            Window(
                entry_name=w["entry_name"],
                required=w.get("required", True),
                start=datetime.fromisoformat(w["start"]) if w.get("start") else None,
                end=datetime.fromisoformat(w["end"]) if w.get("end") else None,
            )
            for w in rec["windows"]
        ),
        conditions=codec.build(rec["conditions_spec"]) if rec.get("conditions_spec") else None,
        priorities=tuple(
            codec.build_priority(p) for p in rec.get("priorities_spec", [])
        ),
        evidence=tuple(
            Evidence(
                eid=e.get("eid", ""),
                source_name=e.get("source_name", ""),
                url=e.get("url", ""),
                quote=e.get("quote", ""),
                located_at=e.get("located_at", ""),
                version_time=datetime.fromisoformat(e["version_time"])
                if e.get("version_time")
                else None,
                verified_at=datetime.fromisoformat(e["verified_at"])
                if e.get("verified_at")
                else None,
            )
            for e in rec.get("evidence", [])
        ),
    )


class OpportunityRepository:
    """机会库:spec 形态 JSON 落盘,加载即重建条件树。"""

    def __init__(self, path: Path):
        self._path = Path(path)

    def save(self, opportunities: list[Opportunity]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"opportunities": [opportunity_to_record(o) for o in opportunities]}
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load(self) -> list[Opportunity]:
        if not self._path.exists():
            return []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return [record_to_opportunity(r) for r in raw.get("opportunities", [])]
