"""显式导入受信任的文件发布库；继承原审核，不重新验证政策语义。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..acquisition.artifact_files import canonical_json
from ..acquisition.publications import PublicationError, PublicationRepository
from ..acquisition.store import SnapshotStorageError, record_to_opportunity
from .publications import TaskPublicationRepository, _record


def _files(directory):
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise PublicationError('发布库目录无效')
    files = sorted(directory.iterdir())
    if any(path.is_symlink() or not path.is_file() or path.suffix != '.json' for path in files):
        raise PublicationError('发布库包含未识别的记录')
    return files


def migrate_file_publications(source: PublicationRepository, target: TaskPublicationRepository) -> dict:
    """在源写锁和目标事务中导入全部版本及当前指针，不复制或覆盖快照。

    目标快照仓库须已可读取相同内容。仅信任调用方选择的历史源；摘要校验不等于重新核验。
    """
    if not isinstance(source, PublicationRepository) or not isinstance(target, TaskPublicationRepository):
        raise ValueError('无效发布仓库')
    if not source.root.is_dir():
        raise PublicationError('源发布库不存在')
    try:
        with source._writer():
            records = {}
            for path in _files(source.root / 'versions'):
                version = path.stem
                try:
                    record = source.read_record(version)
                except (AttributeError, TypeError) as error:
                    raise PublicationError('事实版本记录格式无效') from error
                if not isinstance(record, dict) or not isinstance(record.get('oid'), str) or not record['oid']:
                    raise PublicationError('事实版本缺少有效机会标识')
                if record.get('status') not in ('published', 'published_partial', 'quarantined'):
                    raise PublicationError('事实版本状态无效')
                if record['status'] in ('published', 'published_partial'):
                    try:
                        if record_to_opportunity(record['opportunity']).oid != record['oid']:
                            raise PublicationError('机会记录标识不一致')
                    except (KeyError, TypeError, ValueError) as error:
                        raise PublicationError('机会记录无效') from error
                try:
                    source_sha = record['source']['sha256']
                    datetime.fromisoformat(record['source']['fetchedAt'])
                    src_body = source.snapshots.read(source_sha)
                    target_body = target.snapshots.read(source_sha)
                except (KeyError, TypeError, ValueError, SnapshotStorageError) as error:
                    raise PublicationError('源或目标快照缺失、损坏或元数据无效') from error
                if src_body != target_body:
                    raise PublicationError('源与目标快照不一致')
                records[version] = record

            pointers = {}
            for path in _files(source.root / 'current'):
                pointer = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(pointer, dict) or set(pointer) != {'version'}:
                    raise PublicationError('当前指针格式无效')
                version = pointer['version']
                record = records.get(version) if isinstance(version, str) else None
                if record is None or record['status'] not in ('published', 'published_partial'):
                    raise PublicationError('当前指针引用缺失或隔离版本')
                oid = record['oid']
                if path != source._pointer(oid) or oid in pointers:
                    raise PublicationError('当前指针文件与机会标识不一致')
                try:
                    if record_to_opportunity(record['opportunity']).oid != oid:
                        raise PublicationError('机会记录标识不一致')
                except (KeyError, TypeError, ValueError) as error:
                    raise PublicationError('当前机会记录无效') from error
                pointers[oid] = version

            manifest = {'versions': {version: hashlib.sha256(canonical_json(record)).hexdigest()
                                     for version, record in sorted(records.items())}, 'current': pointers}
            manifest_body = canonical_json(manifest)
            digest = hashlib.sha256(manifest_body).hexdigest()
            with target.store._transaction() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS publication_imports (manifest TEXT PRIMARY KEY, imported_at TEXT NOT NULL, version_count INTEGER NOT NULL, current_count INTEGER NOT NULL)')
                connection.execute('CREATE TABLE IF NOT EXISTS publication_import_manifests (manifest TEXT PRIMARY KEY, body BLOB NOT NULL, FOREIGN KEY(manifest) REFERENCES publication_imports(manifest))')
                for row in connection.execute('SELECT oid,version FROM publication_current'):
                    if pointers.get(row['oid']) != row['version']:
                        raise PublicationError('目标当前指针与源不一致，拒绝覆盖或切换')
                for version, record in records.items():
                    row = connection.execute('SELECT oid,body FROM publication_versions WHERE version=?', (version,)).fetchone()
                    if row:
                        if row['oid'] != record['oid'] or _record(connection, version) != record:
                            raise PublicationError('目标相同版本的审核记录与源不一致')
                    else:
                        connection.execute('INSERT INTO publication_versions VALUES(?,?,?)',
                                           (version, record['oid'], canonical_json(record)))
                for oid, version in pointers.items():
                    row = connection.execute('SELECT version FROM publication_current WHERE oid=?', (oid,)).fetchone()
                    if row and row['version'] != version:
                        raise PublicationError('目标当前版本冲突，拒绝覆盖')
                    if not row:
                        connection.execute('INSERT INTO publication_current VALUES(?,?)', (oid, version))
                # 文件与 SQLite 不共享事务；提交前复验以缩小预检后的缺失窗口。
                for record in records.values():
                    try:
                        target.snapshots.read(record['source']['sha256'])
                    except (KeyError, TypeError, SnapshotStorageError) as error:
                        raise PublicationError('目标快照在迁移期间失效') from error
                connection.execute('INSERT OR IGNORE INTO publication_imports VALUES(?,?,?,?)',
                                   (digest, datetime.now(timezone.utc).isoformat(), len(records), len(pointers)))
                audit = connection.execute('SELECT version_count,current_count FROM publication_imports WHERE manifest=?', (digest,)).fetchone()
                if audit['version_count'] != len(records) or audit['current_count'] != len(pointers):
                    raise PublicationError('目标迁移审计数量不一致')
                connection.execute('INSERT OR IGNORE INTO publication_import_manifests VALUES(?,?)', (digest, manifest_body))
                saved = connection.execute('SELECT body FROM publication_import_manifests WHERE manifest=?', (digest,)).fetchone()['body']
                if not isinstance(saved, bytes) or saved != manifest_body or hashlib.sha256(saved).hexdigest() != digest:
                    raise PublicationError('目标迁移清单损坏')
            return {'manifest': digest, 'versions': len(records), 'current': len(pointers)}
    except (OSError, ValueError, TypeError) as error:
        raise PublicationError('发布库读取或迁移失败') from error
