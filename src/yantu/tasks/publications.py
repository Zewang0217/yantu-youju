"""受任务租约保护的 SQLite 事实发布；与文件版仓储是独立资料库。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from dataclasses import asdict

from ..acquisition.artifact_files import canonical_json
from ..acquisition.fetcher import Snapshot
from ..acquisition.publications import PublicationError, publication_version, validate_current_record
from ..acquisition.store import SnapshotStorageError, record_to_opportunity
from ..acquisition.trusted_pipeline import ConservativeExtractor, OpportunityScope, TrustedPolicyPipeline
from .models import Budget, LeaseLost, StepFailure, StepResult, StepSpec, TaskConflict, TaskStorageError, Usage


class PublicationConflict(PublicationError):
    """当前版本不符合冻结的 CAS 条件，需要重新核验。"""


def publication_input(snapshot, scope, expected_version=None):
    """提交任务时冻结发布范围、快照身份及预期指针。"""
    return {'snapshot': {'url': snapshot.url, 'fetchedAt': snapshot.fetched_at.isoformat(),
                         'statusCode': snapshot.status_code, 'contentType': snapshot.content_type,
                         'sha256': snapshot.sha256, 'finalUrl': snapshot.final_url},
            'scope': asdict(scope), 'expectedVersion': expected_version}


def _record(connection, version):
    row = connection.execute('SELECT body FROM publication_versions WHERE version=?', (version,)).fetchone()
    try:
        value = json.loads(row['body']) if row else None
        if not isinstance(value, dict) or publication_version(value) != version:
            raise PublicationError('事实版本缺失或摘要不一致')
    except (ValueError, TypeError) as error:
        raise PublicationError('事实版本记录不可读') from error
    return value


class _CommitAdapter:
    """仅供 TrustedPolicyPipeline 使用：门禁通过后在回调内提交。"""

    def __init__(self, owner, lease, attempt, usage):
        self.owner, self.snapshots = owner, owner.snapshots
        self.lease, self.attempt, self.usage = lease, attempt, usage

    def write(self, record, *, expected_version):
        version = self.owner._commit(self.lease, self.attempt, self.usage, record, expected_version, self.binding)
        if version is None:
            raise LeaseLost('任务已截止')
        return version


class TaskPublicationRepository:
    """仅接受快照、显式范围和提取器，经既有独立门禁后提交，不接收外部审核记录。"""

    def __init__(self, store, snapshots):
        self.store, self.snapshots = store, snapshots

    def submit(self, snapshot, scope, *, request_id, idempotency_key, expected_version=None,
               budget=Budget(), parent_task_id=None):
        """冻结单步骤任务与快照；仅保存资料，不发布事实。"""
        self.snapshots.save(snapshot)
        return self.store.submit(request_id=request_id, idempotency_key=idempotency_key,
                                 payload={'publication': publication_input(snapshot, scope, expected_version)},
                                 plan=(StepSpec('publish', 'policy_publish', 'v1'),),
                                 budget=budget, parent_task_id=parent_task_id)

    def run(self, task_id, *, owner, lease_seconds=60):
        """执行或恢复单步骤离线发布；由发布事务完成步骤，不走通用 runner。"""
        lease = self.store.claim(task_id, owner=owner, lease_seconds=lease_seconds)
        if lease is None:
            return self.store.inspect(task_id)
        attempt, sent = None, False
        try:
            attempt = self.store.start_step(lease)
            if attempt is None:
                return self.store.inspect(task_id)
            data = self.store.payload(task_id)['publication']
            info = data['snapshot']
            snapshot = Snapshot(info['url'], datetime.fromisoformat(info['fetchedAt']), info['statusCode'],
                                info['contentType'], self.snapshots.read(info['sha256']), info['finalUrl'])
            scope = OpportunityScope(**data['scope'])
            if not self.store.dispatched(lease, attempt):
                return self.store.inspect(task_id)
            sent = True
            request_id = self.store.inspect(task_id)['request_id']
            self.publish(lease, attempt, snapshot, scope, ConservativeExtractor(), request_id=request_id,
                         expected_version=data['expectedVersion'], usage=Usage())
        except LeaseLost:
            return self.store.inspect(task_id)
        except TaskStorageError:
            raise
        except Exception as error:
            if attempt is not None:
                cause = error.__cause__ if isinstance(error, PublicationError) else None
                if isinstance(error, PublicationConflict) or isinstance(cause, PublicationConflict):
                    code = 'PUBLISH_CONFLICT'
                elif isinstance(error, SnapshotStorageError) or isinstance(cause, SnapshotStorageError):
                    code = 'SOURCE_INTEGRITY_FAILED'
                elif isinstance(error, TaskConflict):
                    code = 'INPUT_CHANGED'
                else:
                    code = 'INVARIANT_BROKEN'
                try:
                    self.store.fail(lease, attempt, StepFailure(code, sent=sent, usage=Usage() if sent else None))
                except LeaseLost:
                    pass
            raise
        return self.store.inspect(task_id)

    def _validate_binding(self, connection, task, attempt, binding, request_id):
        if task['request_id'] != request_id:
            raise TaskConflict('发布请求与任务不一致')
        if hashlib.sha256(task['input_json']).hexdigest() != task['input_hash']:
            raise TaskStorageError('任务输入摘要不一致')
        payload = json.loads(task['input_json'])
        if payload.get('publication') != binding:
            raise TaskConflict('发布范围、快照或预期版本与冻结输入不一致')
        step = connection.execute('SELECT handler,version FROM steps WHERE task_id=? AND name=?',
                                  (task['id'], attempt.step_name)).fetchone()
        if (step is None or step['handler'] != 'policy_publish' or step['version'] != 'v1'
                or attempt.handler != step['handler'] or attempt.version != step['version']):
            raise TaskConflict('步骤不具备发布权限')

    def publish(self, lease, attempt, snapshot, scope, extractor, *, request_id, expected_version=None, usage=None):
        if type(extractor) is not ConservativeExtractor:
            raise ValueError('当前仅允许不调用模型的保守提取器')
        binding = publication_input(snapshot, scope, expected_version)
        with self.store._transaction() as connection:
            task = self.store._ready_to_finish(connection, lease, attempt, self.store._now())
            if task is not None:
                self._validate_binding(connection, task, attempt, binding, request_id)
                self.store._assert_commit_window(task)
        if task is None:
            raise LeaseLost('任务已截止')
        adapter = _CommitAdapter(self, lease, attempt, usage)
        adapter.binding = binding
        return TrustedPolicyPipeline(self.snapshots, adapter, extractor).run(
            snapshot, scope, request_id=request_id, expected_version=expected_version)

    def _commit(self, lease, attempt, usage, record, expected_version, binding):
        # 私有回调只能由上面的 pipeline 提供记录；重新读取快照位于同一发布事务中。
        version = publication_version(record)
        body = canonical_json(record)
        if len(body) > 8_000_000:
            raise PublicationError('事实记录超过预算')
        with self.store._transaction() as connection:
            now = self.store._now()
            task = self.store._ready_to_finish(connection, lease, attempt, now)
            if task is None:
                return None
            self._validate_binding(connection, task, attempt, binding, record['requestId'])
            try:
                self.snapshots.read(record['source']['sha256'])
            except SnapshotStorageError as error:
                raise PublicationError('源快照无法验证') from error
            oid, status = record['oid'], record['status']
            if status not in ('published', 'published_partial', 'quarantined'):
                raise PublicationError('未知事实状态')
            row = connection.execute('SELECT version FROM publication_current WHERE oid=?', (oid,)).fetchone()
            current = row['version'] if row else None
            if current != version and current != expected_version:
                raise PublicationConflict('当前事实版本已变更，必须重新核验后发布')
            if current:
                prior = _record(connection, current)
                if prior.get('oid') != oid or prior.get('status') not in ('published', 'published_partial'):
                    raise PublicationError('当前事实指针无效')
                if status in ('published', 'published_partial'):
                    try:
                        if datetime.fromisoformat(record['source']['fetchedAt']) < datetime.fromisoformat(prior['source']['fetchedAt']):
                            raise PublicationConflict('较早获取的快照不能替换较新来源')
                    except (ValueError, TypeError, KeyError) as error:
                        raise PublicationError('来源时间不可比较') from error
            connection.execute('INSERT OR IGNORE INTO publication_versions VALUES(?,?,?)', (version, oid, body))
            if _record(connection, version).get('oid') != oid:
                raise PublicationError('事实版本归属不一致')
            if status in ('published', 'published_partial') and current != version:
                connection.execute('INSERT INTO publication_current VALUES(?,?) ON CONFLICT(oid) DO UPDATE SET version=excluded.version', (oid, version))
            result = StepResult({'publicationVersion': version, 'publicationStatus': status}, usage, status != 'published')
            self.store._event(connection, task, 'publication_committed', now, status=status, step=attempt.step_name, attempt=attempt.number)
            self.store._finish_in_transaction(connection, task, attempt, result, now)
            self.store._assert_commit_window(task)
            return version

    def current_version(self, oid):
        with self.store._transaction(write=False) as connection:
            row = connection.execute('SELECT version FROM publication_current WHERE oid=?', (oid,)).fetchone()
            return row['version'] if row else None

    def read_record(self, version):
        if not isinstance(version, str) or len(version) != 64 or any(c not in '0123456789abcdef' for c in version):
            raise PublicationError('非法版本引用')
        with self.store._transaction(write=False) as connection:
            return _record(connection, version)

    def current_entry(self, oid):
        """在单次读事务内取得当前版本及记录，避免读者跨版本拼接。"""
        with self.store._transaction(write=False) as connection:
            row = connection.execute('SELECT version FROM publication_current WHERE oid=?', (oid,)).fetchone()
            if row is None:
                return None
            record = _record(connection, row['version'])
            validate_current_record(record, oid, self.snapshots, snapshot_error='当前事实的源快照不可验证')
            return row['version'], record

    def current_record(self, oid):
        entry = self.current_entry(oid)
        return None if entry is None else entry[1]

    def load(self, oid):
        record = self.current_record(oid)
        return None if record is None else record_to_opportunity(record['opportunity'])
