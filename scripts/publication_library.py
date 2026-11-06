"""显式迁移旧文件发布库，或读取 SQLite 当前审核记录；不自动切换已有读者。"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from yantu.acquisition.error_events import log_failure
from yantu.acquisition.publications import PublicationRepository
from yantu.acquisition.store import SnapshotStore
from yantu.tasks.publication_migration import migrate_file_publications
from yantu.tasks.publication_reader import ReadOnlyTaskStore, open_publication_reader
from yantu.tasks.publications import TaskPublicationRepository
from yantu.tasks.store import TaskStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    migrate = commands.add_parser('migrate', help='显式复制已审核的文件版本与当前指针')
    migrate.add_argument('--source', type=Path, required=True)
    for sub in (migrate, commands.add_parser('show', help='校验并读取当前版本')):
        sub.add_argument('--db', type=Path, required=True)
        sub.add_argument('--snapshots', type=Path, required=True)
        if sub is not migrate:
            sub.add_argument('--oid', required=True)
    args = parser.parse_args()
    if not args.snapshots.is_dir():
        raise ValueError('快照目录不存在')
    if args.command == 'migrate' and not args.source.is_dir():
        raise ValueError('源发布库不存在')
    if args.command == 'show' and not args.db.is_file():
        raise ValueError('任务数据库不存在')
    snapshots = SnapshotStore(args.snapshots)
    if args.command == 'migrate':
        if args.db.exists():
            ReadOnlyTaskStore(args.db).validate()
        target = TaskPublicationRepository(TaskStore(args.db), snapshots)
        source = PublicationRepository(args.source, snapshots)
        value = migrate_file_publications(source, target)
    else:
        target = open_publication_reader(args.db, snapshots)
        entry = target.current_entry(args.oid)
        if entry is None:
            value = {'oid': args.oid, 'version': None, 'record': None}
        else:
            version, record = entry
            value = {'oid': args.oid, 'version': version, 'record': record}
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        log_failure(logging.getLogger(__name__), error, event='publication_library_failed')
        raise SystemExit(1) from None
