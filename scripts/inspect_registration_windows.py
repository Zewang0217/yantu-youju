"""离线核验资料包中各文档的报名时间及定位；不合并阶段或发布机会。"""

import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime

from yantu.acquisition.artifact_files import atomic_write, canonical_json
from yantu.acquisition.document_bundle import load_bundle_record
from yantu.acquisition.policy_verification import PolicyValidationError
from yantu.acquisition.registration_window import extract_registration_window
from yantu.acquisition.store import SnapshotStore
from yantu.acquisition.parse_models import PageText
from yantu.acquisition.source_evidence import claim_record, source_quality


def inspect_windows(bundle_path: Path) -> dict:
    """先验证包/源字节，再逐来源处理；不把别的通知时间拷入当前文档。"""
    bundle = load_bundle_record(bundle_path, SnapshotStore(bundle_path.parent / "snapshots"))
    results = []
    for document in bundle["documents"]:
        row = {"url": document["url"], "snapshotSha256": document["snapshot_sha256"], "status": "not_readable", "claims": []}
        parsed = document.get("parse_result")
        quality = source_quality(parsed)
        row.update(sourceParseStatus=quality["parseStatus"], sourceIssues=quality["issues"], sourceNotes=quality["notes"],
                   **{key: quality[key] for key in ("declaredPageCount", "parsedPageCount", "unprocessedPageCount", "pageGaps", "parser")})
        if parsed and parsed["status"] not in ("unsupported", "failed"):
            text = parsed["text"]
            row["textSha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            try:
                window, claims = extract_registration_window(text)
            except PolicyValidationError as error:
                row.update(status="unresolved", reason=str(error))
            else:
                row.update(status="extracted" if claims else "not_found", start=window.start.isoformat() if window.start else None,
                           end=window.end.isoformat() if window.end else None)
                for claim in claims:
                    pages = tuple(PageText(**page) for page in parsed["pages"])
                    row["claims"].append({**claim_record(claim, text, pages), "precision": "minute"})
        results.append(row)
    return {"schemaVersion": "registration-window-inspection-v1", "author": "Codex（AI助手）", "created": datetime.now().astimezone().isoformat(),
            "requestId": bundle["requestId"], "inputBundleDigest": bundle["recordDigest"], "dateParserVersion": "text-dates-v2-precision",
            "inputBundleStatus": bundle["status"], "inputGaps": bundle["gaps"],
            "publicationStatus": "not_published", "applicability": "not_evaluated", "results": results,
            "limitation": "仅在单篇文档识别的报名段内核验时间句；不证明所有通道/阶段、专业适用性或当前开放状态"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    report = inspect_windows(args.bundle)
    output = args.bundle.parent / "window_fields.json"
    atomic_write(output, canonical_json(report))
    print(json.dumps({"output": str(output.resolve()), "documents": len(report["results"]),
                      "statuses": [row["status"] for row in report["results"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
