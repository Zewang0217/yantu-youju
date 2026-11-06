"""SQLite任务账本；步骤产物、状态、预算与本地事件在一个事务中提交。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator

from ..acquisition.artifact_files import canonical_json
from .models import (TERMINAL, RETRYABLE_CODES, Attempt, Budget, Lease, LeaseLost, StepFailure, StepResult,
                     StepSpec, TaskConflict, TaskStorageError, Usage, identifier, seconds)

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL, budget_json TEXT NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), key_hash TEXT UNIQUE NOT NULL,
 input_hash TEXT NOT NULL, input_json BLOB NOT NULL, plan_json BLOB NOT NULL, parent_id TEXT REFERENCES tasks(id),
 status TEXT NOT NULL, reason TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 owner TEXT, epoch INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS steps (
 task_id TEXT NOT NULL REFERENCES tasks(id), name TEXT NOT NULL, ordinal INTEGER NOT NULL, handler TEXT NOT NULL,
 version TEXT NOT NULL, estimate_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 output_hash TEXT, partial INTEGER NOT NULL DEFAULT 0, error_code TEXT, PRIMARY KEY(task_id,name), UNIQUE(task_id,ordinal));
CREATE TABLE IF NOT EXISTS attempts (
 task_id TEXT NOT NULL, step_name TEXT NOT NULL, number INTEGER NOT NULL, call_id TEXT UNIQUE NOT NULL,
 epoch INTEGER NOT NULL, status TEXT NOT NULL, started REAL NOT NULL, ended REAL, error_code TEXT,
 PRIMARY KEY(task_id,step_name,number), FOREIGN KEY(task_id,step_name) REFERENCES steps(task_id,name));
CREATE TABLE IF NOT EXISTS calls (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), task_id TEXT NOT NULL REFERENCES tasks(id),
 status TEXT NOT NULL, estimate_json TEXT NOT NULL, actual_json TEXT);
CREATE TABLE IF NOT EXISTS artifacts (hash TEXT PRIMARY KEY, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS publication_versions (
 version TEXT PRIMARY KEY, oid TEXT NOT NULL, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS publication_current (
 oid TEXT PRIMARY KEY, version TEXT NOT NULL REFERENCES publication_versions(version));
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, task_id TEXT NOT NULL, step_name TEXT,
 attempt INTEGER, event TEXT NOT NULL, status TEXT, error_code TEXT, time REAL NOT NULL);
CREATE TABLE IF NOT EXISTS child_grants (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), parent_task_id TEXT NOT NULL REFERENCES tasks(id),
 run_id TEXT NOT NULL REFERENCES runs(id), grant_json TEXT NOT NULL, cost_unit TEXT NOT NULL);
'''


def _freeze_plan(plan):
    """冻结执行计划；旧只读计划不得增加字段，否则已提交任务的幂等键会失效。"""
    steps = []
    for step in plan:
        value = asdict(step)
        if step.replay_policy == 'replay_safe':
            value.pop('replay_policy')
            value.pop('cost_unit')
        steps.append(value)
    return canonical_json({'steps': steps})


def _insert_steps(connection, task_id, plan):
    """在调用者事务中写入冻结计划对应的步骤，不单独提交。"""
    for ordinal, step in enumerate(plan):
        connection.execute('INSERT INTO steps(task_id,name,ordinal,handler,version,estimate_json) VALUES(?,?,?,?,?,?)',
                           (task_id, step.name, ordinal, step.handler, step.version, canonical_json(asdict(step.estimate)).decode()))


def _exceeds(limits, used, held, *reservations):
    """只比较同一额度的三个维度；预留与已用不能重复传入。"""
    return any(used[name] + held[name] + sum(item[name] for item in reservations) > limits[name] for name in used)


class TaskStore:
    """单机事务仓储；短事务不跨工具调用，SQLite锁不充当长期worker租约。"""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as connection:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, 1):
                    raise TaskStorageError('不支持的任务账本版本')
                connection.execute('PRAGMA journal_mode=WAL')
                connection.executescript(_SCHEMA)
                connection.execute('PRAGMA user_version=1')
                connection.commit()
        except sqlite3.Error as error:
            raise TaskStorageError('任务账本初始化失败') from error

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            if connection is not None:
                connection.rollback()
            raise TaskStorageError('任务事务未确认提交') from error
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _now(self) -> float:
        now = self.clock()
        seconds(now)
        return now

    @staticmethod
    def _task(connection, task_id):
        row = connection.execute('SELECT t.*,r.request_id,r.budget_json,r.deadline FROM tasks t JOIN runs r ON r.id=t.run_id WHERE t.id=?', (task_id,)).fetchone()
        if row is None:
            raise TaskConflict('任务不存在')
        return row

    @staticmethod
    def _event(connection, task, event, now, *, status=None, code=None, step=None, attempt=None):
        connection.execute('INSERT INTO events(run_id,task_id,step_name,attempt,event,status,error_code,time) VALUES(?,?,?,?,?,?,?,?)',
            (task['run_id'], task['id'], step, attempt, event, status, code, now))

    @staticmethod
    def _charges(connection, run_id, task_id=None):
        used = dict.fromkeys(('tool_calls', 'tokens', 'cost_units'), 0)
        held = dict(used)
        pending = 0
        query = 'SELECT status,estimate_json,actual_json FROM calls WHERE run_id=?'
        args = [run_id]
        if task_id is not None:
            query += ' AND task_id=?'
            args.append(task_id)
        for call in connection.execute(query, args):
            if call['status'] == 'settled':
                usage, destination = json.loads(call['actual_json']), used
            elif call['status'] in ('reserved', 'dispatched', 'usage_pending'):
                usage, destination = json.loads(call['estimate_json']), held
                pending += call['status'] == 'usage_pending'
            else:
                continue
            for name in used:
                destination[name] += usage[name]
        return used, held, pending

    @classmethod
    def _unspent_grants(cls, connection, run_id, *, excluding=None):
        """已发给活跃子任务但尚未花费的额度；调用本身由run账本另计。"""
        total = dict.fromkeys(('tool_calls', 'tokens', 'cost_units'), 0)
        rows = connection.execute('SELECT g.*,t.status FROM child_grants g JOIN tasks t ON t.id=g.task_id WHERE g.run_id=?', (run_id,))
        for row in rows:
            if row['task_id'] == excluding or row['status'] in TERMINAL:
                continue
            grant = json.loads(row['grant_json'])
            used, held, _ = cls._charges(connection, run_id, row['task_id'])
            for name in total:
                total[name] += max(0, grant[name] - used[name] - held[name])
        return total

    def submit(self, *, request_id: str, idempotency_key: str, payload: dict, plan: tuple[StepSpec, ...],
               budget: Budget = Budget(), parent_task_id: str | None = None) -> str:
        """原子创建run及根任务；相同键必须匹配冻结输入、步骤版本、预算和重跑关联。"""
        identifier(request_id)
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 512:
            raise ValueError('无效幂等键')
        if (not isinstance(payload, dict) or not isinstance(budget, Budget) or not 1 <= len(plan) <= 32
                or any(not isinstance(step, StepSpec) for step in plan) or len({step.name for step in plan}) != len(plan)):
            raise ValueError('无效任务输入或步骤计划')
        if any(step.replay_policy == 'at_most_once' for step in plan) and len(plan) != 1:
            raise ValueError('一次性任务只支持单步骤')
        input_bytes, plan_bytes = canonical_json(payload), _freeze_plan(plan)
        if len(input_bytes) > 1_000_000:
            raise ValueError('任务输入超过预算')
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        input_hash = hashlib.sha256(input_bytes).hexdigest()
        budget_json = canonical_json(asdict(budget)).decode()
        with self._transaction() as connection:
            now = self._now()
            prior = connection.execute('SELECT t.*,r.budget_json FROM tasks t JOIN runs r ON t.run_id=r.id WHERE key_hash=?', (key_hash,)).fetchone()
            if prior:
                if (prior['input_hash'] != input_hash or prior['plan_json'] != plan_bytes or prior['budget_json'] != budget_json
                        or prior['parent_id'] != parent_task_id):
                    raise TaskConflict('幂等键对应的输入或执行配置已变化')
                return prior['id']
            if parent_task_id is not None:
                self._task(connection, parent_task_id)
            run_id, task_id = uuid.uuid4().hex, uuid.uuid4().hex
            connection.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (run_id, request_id, budget_json, now, now + budget.wall_seconds))
            connection.execute('INSERT INTO tasks(id,run_id,key_hash,input_hash,input_json,plan_json,parent_id,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (task_id, run_id, key_hash, input_hash, input_bytes, plan_bytes, parent_task_id, 'queued', now, now))
            _insert_steps(connection, task_id, plan)
            task = self._task(connection, task_id)
            self._event(connection, task, 'task_submitted', now, status='queued')
            return task_id

    def submit_child(self, lease: Lease, *, idempotency_key: str, payload: dict,
                     plan: tuple[StepSpec, ...], quota: Usage, cost_unit: str) -> str | None:
        """在父租约下原子下发单层子任务额度；不复用旧parent_task_id重跑语义。"""
        identifier(cost_unit)
        if (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 512
                or not isinstance(payload, dict) or not isinstance(quota, Usage)
                or not isinstance(plan, tuple) or not 1 <= len(plan) <= 32
                or any(not isinstance(step, StepSpec) for step in plan)
                or len({step.name for step in plan}) != len(plan)):
            raise ValueError('无效子任务输入或配额')
        if any(step.replay_policy == 'at_most_once' for step in plan) and len(plan) != 1:
            raise ValueError('一次性任务只支持单步骤')
        if any(step.cost_unit is not None and step.cost_unit != cost_unit for step in plan):
            raise ValueError('子任务成本单位不一致')
        input_bytes = canonical_json(payload)
        if len(input_bytes) > 1_000_000:
            raise ValueError('任务输入超过预算')
        plan_bytes = _freeze_plan(plan)
        grant_bytes = canonical_json(asdict(quota)).decode()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        input_hash = hashlib.sha256(input_bytes).hexdigest()
        with self._transaction() as connection:
            now = self._now()
            parent = self._active_task(connection, lease, now)
            if parent is None:
                return None
            if connection.execute('SELECT 1 FROM child_grants WHERE task_id=?', (parent['id'],)).fetchone():
                raise TaskConflict('当前仅支持一层子任务')
            prior = connection.execute('SELECT t.*,g.grant_json,g.cost_unit FROM tasks t LEFT JOIN child_grants g ON g.task_id=t.id WHERE t.key_hash=?', (key_hash,)).fetchone()
            if prior is not None:
                if (prior['parent_id'] != parent['id'] or prior['run_id'] != parent['run_id'] or prior['input_hash'] != input_hash
                        or prior['plan_json'] != plan_bytes or prior['grant_json'] != grant_bytes or prior['cost_unit'] != cost_unit):
                    raise TaskConflict('幂等键对应的子任务配置已变化')
                return prior['id']
            units = {row['cost_unit'] for row in connection.execute('SELECT cost_unit FROM child_grants WHERE run_id=?', (parent['run_id'],))}
            root_steps = json.loads(parent['plan_json'])['steps']
            units.update(step['cost_unit'] for step in root_steps if step.get('cost_unit'))
            if units and units != {cost_unit}:
                raise TaskConflict('运行的成本单位不一致')
            used, held, _ = self._charges(connection, parent['run_id'])
            if not units and (used['cost_units'] or held['cost_units'] or any(step['estimate']['cost_units'] for step in root_steps)):
                raise TaskConflict('根任务现有成本单位未冻结')
            allocated = self._unspent_grants(connection, parent['run_id'])
            limits = json.loads(parent['budget_json'])
            grant = asdict(quota)
            if _exceeds(limits, used, held, allocated, grant):
                raise TaskConflict('父任务可分配额度不足')
            child_id = uuid.uuid4().hex
            connection.execute('INSERT INTO tasks(id,run_id,key_hash,input_hash,input_json,plan_json,parent_id,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (child_id, parent['run_id'], key_hash, input_hash, input_bytes, plan_bytes, parent['id'], 'queued', now, now))
            connection.execute('INSERT INTO child_grants VALUES(?,?,?,?,?)', (child_id, parent['id'], parent['run_id'], grant_bytes, cost_unit))
            _insert_steps(connection, child_id, plan)
            self._event(connection, self._task(connection, child_id), 'child_submitted', now, status='queued')
            self._assert_commit_window(parent)
            return child_id

    def _interrupt(self, connection, task, now, code):
        active = connection.execute('SELECT * FROM attempts WHERE task_id=? AND status="running"', (task['id'],)).fetchall()
        unsafe = False
        for attempt in active:
            call = connection.execute('SELECT status FROM calls WHERE id=?', (attempt['call_id'],)).fetchone()
            policy = self._step_policy(task, attempt['step_name'])
            unsafe |= policy.get('replay_policy', 'replay_safe') == 'at_most_once' and call['status'] == 'dispatched'
            connection.execute('UPDATE calls SET status=CASE WHEN status="reserved" THEN "released" ELSE "usage_pending" END WHERE id=? AND status IN ("reserved","dispatched")', (attempt['call_id'],))
            connection.execute('UPDATE attempts SET status="interrupted",ended=?,error_code=? WHERE call_id=?', (now, code, attempt['call_id']))
            connection.execute('UPDATE steps SET status="queued",error_code=? WHERE task_id=? AND name=?', (code, task['id'], attempt['step_name']))
            self._event(connection, task, 'attempt_interrupted', now, code=code, step=attempt['step_name'], attempt=attempt['number'])
        return unsafe

    @staticmethod
    def _step_policy(task, step_name):
        steps = json.loads(task['plan_json'])['steps']
        matching = [step for step in steps if step['name'] == step_name]
        if len(matching) != 1:
            raise TaskStorageError('任务计划损坏')
        return matching[0]

    def _stop(self, connection, task, now, code, *, cancelled=False, finalize_parent=True):
        children = connection.execute('SELECT task_id FROM child_grants WHERE parent_task_id=?', (task['id'],)).fetchall()
        for child in children:
            child_task = self._task(connection, child['task_id'])
            if child_task['status'] not in TERMINAL:
                self._stop(connection, child_task, now, code, cancelled=cancelled, finalize_parent=False)
        self._interrupt(connection, task, now, code)
        completed = connection.execute('SELECT COUNT(*) FROM steps WHERE task_id=? AND status="completed"', (task['id'],)).fetchone()[0]
        status = 'cancelled' if cancelled else 'partial' if completed else 'failed'
        connection.execute('UPDATE steps SET status=?,error_code=? WHERE task_id=? AND status!="completed"',
                           ('cancelled' if cancelled else 'failed', code, task['id']))
        connection.execute('UPDATE tasks SET status=?,reason=?,owner=NULL,epoch=epoch+1,lease_until=0,updated=? WHERE id=?', (status, code, now, task['id']))
        self._event(connection, task, 'task_stopped', now, status=status, code=code)
        if finalize_parent:
            self._finish_waiting_parent(connection, task, now)

    def _finish_waiting_parent(self, connection, child, now):
        grant = connection.execute('SELECT parent_task_id FROM child_grants WHERE task_id=?', (child['id'],)).fetchone()
        if grant is None:
            return
        parent = self._task(connection, grant['parent_task_id'])
        if parent['status'] != 'waiting_input':
            return
        children = connection.execute('SELECT t.status FROM child_grants g JOIN tasks t ON t.id=g.task_id WHERE g.parent_task_id=?', (parent['id'],)).fetchall()
        if any(row['status'] not in TERMINAL for row in children):
            return
        own_partial = connection.execute('SELECT MAX(partial) FROM steps WHERE task_id=?', (parent['id'],)).fetchone()[0]
        used, held, _ = self._charges(connection, parent['run_id'])
        limits = json.loads(parent['budget_json'])
        overrun = _exceeds(limits, used, held)
        status = 'partial' if own_partial or overrun or any(row['status'] != 'completed' for row in children) else 'completed'
        connection.execute('UPDATE tasks SET status=?,reason=NULL,owner=NULL,lease_until=0,next_at=0,updated=? WHERE id=?', (status, now, parent['id']))
        self._event(connection, parent, 'task_finished', now, status=status)

    @staticmethod
    def _owns(task, lease, now, *, allow_expired=False):
        if (task['status'] != 'running' or task['owner'] != lease.owner or task['epoch'] != lease.epoch
                or (not allow_expired and task['lease_until'] <= now)):
            raise LeaseLost('任务租约已失效')

    def _active_task(self, connection, lease: Lease, now: float):
        """统一当前owner/epoch、绝对截止与租约检查；截止终止也属于调用者事务。"""
        task = self._task(connection, lease.task_id)
        self._owns(task, lease, now, allow_expired=True)
        if task['deadline'] <= now:
            grant = connection.execute('SELECT parent_task_id FROM child_grants WHERE task_id=?', (task['id'],)).fetchone()
            self._stop(connection, self._task(connection, grant['parent_task_id']) if grant else task, now, 'DEADLINE_EXCEEDED')
            return None
        self._owns(task, lease, now)
        return task

    def _assert_commit_window(self, task, *, lease_until: float | None = None) -> None:
        """产物/审计写入也可能耗时；事务末尾失效必须回滚，不能提交过期结果。"""
        now = self._now()
        expiry = task['lease_until'] if lease_until is None else lease_until
        if now >= task['deadline'] or now >= expiry:
            raise LeaseLost('事务提交前租约或截止已失效')

    def claim(self, task_id: str, *, owner: str, lease_seconds: float = 60, replay_policy: str = 'replay_safe') -> Lease | None:
        """取得或回收过期租约；尚未到退避时间或其他worker仍有效时返回None。"""
        identifier(owner)
        seconds(lease_seconds)
        if not 0 < lease_seconds <= 3600:
            raise ValueError('无效租约时长')
        with self._transaction() as connection:
            now = self._now()
            task = self._task(connection, task_id)
            if task['status'] in TERMINAL:
                return None
            policies = {step.get('replay_policy', 'replay_safe') for step in json.loads(task['plan_json'])['steps']}
            if policies != {replay_policy}:
                return None
            if task['deadline'] <= now:
                grant = connection.execute('SELECT parent_task_id FROM child_grants WHERE task_id=?', (task['id'],)).fetchone()
                self._stop(connection, self._task(connection, grant['parent_task_id']) if grant else task, now, 'DEADLINE_EXCEEDED')
                return None
            if task['status'] == 'waiting_input':
                return None
            if task['status'] == 'running' and task['lease_until'] > now:
                return None
            if task['status'] == 'retry_wait' and task['next_at'] > now:
                return None
            if task['status'] == 'running':
                if self._interrupt(connection, task, now, 'WORKER_LOST'):
                    self._stop(connection, task, now, 'WORKER_LOST')
                    return None
            epoch = task['epoch'] + 1
            connection.execute('UPDATE tasks SET status="running",owner=?,epoch=?,lease_until=?,updated=? WHERE id=?', (owner, epoch, now + lease_seconds, now, task_id))
            self._event(connection, task, 'task_claimed', now, status='running')
            self._assert_commit_window(task, lease_until=now + lease_seconds)
            return Lease(task_id, owner, epoch)

    def heartbeat(self, lease: Lease, *, lease_seconds: float = 60) -> bool:
        """协作式检查取消/截止并续租；已过期的worker不能用心跳复活。"""
        seconds(lease_seconds)
        if not 0 < lease_seconds <= 3600:
            raise ValueError('无效租约时长')
        with self._transaction() as connection:
            now = self._now()
            task = self._active_task(connection, lease, now)
            if task is None:
                return False
            connection.execute('UPDATE tasks SET lease_until=?,updated=? WHERE id=?', (now + lease_seconds, now, task['id']))
            self._assert_commit_window(task, lease_until=now + lease_seconds)
            return True

    def start_step(self, lease: Lease) -> Attempt | None:
        """同事务创建尝试和预算预留；预算不足不发出新调用并保留终止原因。"""
        with self._transaction() as connection:
            now = self._now()
            task = self._active_task(connection, lease, now)
            if task is None:
                return None
            step = connection.execute('SELECT * FROM steps WHERE task_id=? AND status!="completed" ORDER BY ordinal LIMIT 1', (task['id'],)).fetchone()
            if step is None or step['status'] == 'running':
                raise TaskConflict('任务步骤状态不允许开始')
            budget, estimate = Budget(**json.loads(task['budget_json'])), json.loads(step['estimate_json'])
            if step['attempts'] >= budget.max_attempts:
                self._stop(connection, task, now, 'RETRY_EXHAUSTED')
                return None
            used, held, _ = self._charges(connection, task['run_id'])
            child = connection.execute('SELECT grant_json FROM child_grants WHERE task_id=?', (task['id'],)).fetchone()
            allocated = self._unspent_grants(connection, task['run_id'], excluding=task['id'] if child else None)
            exceeds_root = _exceeds(asdict(budget), used, held, allocated, estimate)
            exceeds_child = False
            if child:
                child_used, child_held, _ = self._charges(connection, task['run_id'], task['id'])
                grant = json.loads(child['grant_json'])
                exceeds_child = _exceeds(grant, child_used, child_held, estimate)
            if exceeds_root or exceeds_child:
                self._stop(connection, task, now, 'BUDGET_EXHAUSTED')
                return None
            call_id, number = uuid.uuid4().hex, step['attempts'] + 1
            connection.execute('INSERT INTO calls VALUES(?,?,?,?,?,NULL)', (call_id, task['run_id'], task['id'], 'reserved', step['estimate_json']))
            connection.execute('INSERT INTO attempts VALUES(?,?,?,?,?,"running",?,NULL,NULL)', (task['id'], step['name'], number, call_id, lease.epoch, now))
            connection.execute('UPDATE steps SET status="running",attempts=?,error_code=NULL WHERE task_id=? AND name=?', (number, task['id'], step['name']))
            self._event(connection, task, 'attempt_reserved', now, status='running', step=step['name'], attempt=number)
            self._assert_commit_window(task)
            return Attempt(task['id'], step['name'], number, call_id, step['handler'], step['version'])

    def dispatched(self, lease: Lease, attempt: Attempt, *, replay_policy: str = 'replay_safe', cost_unit: str | None = None) -> bool:
        """调用前落盘发送状态；此后发生崩溃必须保守保留未知用量。"""
        with self._transaction() as connection:
            now = self._now()
            task = self._active_task(connection, lease, now)
            if task is None:
                return False
            self._active_attempt(connection, lease, attempt)
            spec = self._step_policy(task, attempt.step_name)
            if (spec.get('replay_policy', 'replay_safe') != replay_policy
                    or spec.get('cost_unit') != cost_unit):
                raise TaskConflict('调用策略或成本单位不匹配')
            changed = connection.execute('UPDATE calls SET status="dispatched" WHERE id=? AND status="reserved"', (attempt.call_id,)).rowcount
            if changed != 1:
                raise TaskConflict('调用已发出，不能再次执行')
            self._event(connection, task, 'call_dispatched', now, step=attempt.step_name, attempt=attempt.number)
            self._assert_commit_window(task)
            return True

    @staticmethod
    def _active_attempt(connection, lease, attempt):
        row = connection.execute('SELECT * FROM attempts WHERE call_id=?', (attempt.call_id,)).fetchone()
        if (row is None or attempt.task_id != lease.task_id or row['task_id'] != lease.task_id or row['epoch'] != lease.epoch or row['status'] != 'running'
                or row['step_name'] != attempt.step_name or row['number'] != attempt.number):
            raise LeaseLost('尝试已无提交权')
        return row

    @classmethod
    def _settle(cls, connection, call_id, usage, *, sent=True):
        if usage is not None:
            call = connection.execute('SELECT run_id,task_id FROM calls WHERE id=?', (call_id,)).fetchone()
            if usage.cost_units and connection.execute('SELECT 1 FROM child_grants WHERE run_id=?', (call['run_id'],)).fetchone():
                grant = connection.execute('SELECT cost_unit FROM child_grants WHERE task_id=?', (call['task_id'],)).fetchone()
                if grant is None:
                    task = cls._task(connection, call['task_id'])
                    attempt = connection.execute('SELECT step_name FROM attempts WHERE call_id=?', (call_id,)).fetchone()
                    if not cls._step_policy(task, attempt['step_name']).get('cost_unit'):
                        raise TaskConflict('根任务实际成本缺少冻结单位')
            connection.execute('UPDATE calls SET status="settled",actual_json=? WHERE id=?', (canonical_json(asdict(usage)).decode(), call_id))
        else:
            connection.execute('UPDATE calls SET status=? WHERE id=?', ('usage_pending' if sent else 'released', call_id))

    @staticmethod
    def _result_body(result: StepResult):
        if (not isinstance(result, StepResult) or not isinstance(result.value, dict)
                or (result.usage is not None and not isinstance(result.usage, Usage))
                or type(result.partial) is not bool):
            raise ValueError('无效步骤结果')
        body = canonical_json(result.value)
        if len(body) > 8_000_000:
            raise ValueError('步骤产物超过预算')
        return body, hashlib.sha256(body).hexdigest()

    def _finish_in_transaction(self, connection, task, attempt, result, now):
        body, digest = self._result_body(result)
        connection.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?)', (digest, body))
        if connection.execute('SELECT body FROM artifacts WHERE hash=?', (digest,)).fetchone()[0] != body:
            raise TaskStorageError('已存在的任务产物损坏')
        self._settle(connection, attempt.call_id, result.usage)
        connection.execute('UPDATE attempts SET status="completed",ended=? WHERE call_id=?', (now, attempt.call_id))
        connection.execute('UPDATE steps SET status="completed",output_hash=?,partial=? WHERE task_id=? AND name=?', (digest, int(result.partial), task['id'], attempt.step_name))
        self._event(connection, task, 'step_completed', now, status='completed', step=attempt.step_name, attempt=attempt.number)
        remaining = connection.execute('SELECT COUNT(*) FROM steps WHERE task_id=? AND status!="completed"', (task['id'],)).fetchone()[0]
        if not remaining:
            partial = connection.execute('SELECT MAX(partial) FROM steps WHERE task_id=?', (task['id'],)).fetchone()[0]
            used, held, _ = self._charges(connection, task['run_id'])
            limits = json.loads(task['budget_json'])
            overrun = _exceeds(limits, used, held)
            grant_row = connection.execute('SELECT grant_json FROM child_grants WHERE task_id=?', (task['id'],)).fetchone()
            if grant_row:
                child_used, child_held, _ = self._charges(connection, task['run_id'], task['id'])
                grant = json.loads(grant_row['grant_json'])
                overrun = overrun or _exceeds(grant, child_used, child_held)
            children = connection.execute('SELECT t.status FROM child_grants g JOIN tasks t ON t.id=g.task_id WHERE g.parent_task_id=?', (task['id'],)).fetchall()
            if any(row['status'] not in TERMINAL for row in children):
                connection.execute('UPDATE tasks SET status="waiting_input",owner=NULL,lease_until=0,next_at=0,updated=? WHERE id=?', (now, task['id']))
                self._event(connection, task, 'children_pending', now, status='waiting_input')
                return
            status = 'partial' if partial or overrun or any(row['status'] != 'completed' for row in children) else 'completed'
            connection.execute('UPDATE tasks SET status=?,reason=NULL,owner=NULL,lease_until=0,next_at=0,updated=? WHERE id=?', (status, now, task['id']))
            self._event(connection, task, 'task_finished', now, status=status)
            self._finish_waiting_parent(connection, task, now)

    def _ready_to_finish(self, connection, lease, attempt, now):
        task = self._active_task(connection, lease, now)
        if task is None:
            return None
        self._active_attempt(connection, lease, attempt)
        if connection.execute('SELECT status FROM calls WHERE id=?', (attempt.call_id,)).fetchone()[0] != 'dispatched':
            raise TaskConflict('调用尚未发出，不能提交工具结果')
        return task

    def finish(self, lease: Lease, attempt: Attempt, result: StepResult) -> bool:
        """原子写不可变产物、预算结算、步骤状态与事件；用量未知保留预算。"""
        self._result_body(result)
        with self._transaction() as connection:
            now = self._now()
            task = self._ready_to_finish(connection, lease, attempt, now)
            if task is None:
                return False
            self._finish_in_transaction(connection, task, attempt, result, now)
            self._assert_commit_window(task)
            return True

    def release(self, lease: Lease) -> None:
        """在步骤检查点主动让出执行权；正在运行的调用不能被当作已暂停。"""
        with self._transaction() as connection:
            now = self._now()
            task = self._task(connection, lease.task_id)
            self._owns(task, lease, now)
            active = connection.execute('SELECT COUNT(*) FROM attempts WHERE task_id=? AND status="running"', (task['id'],)).fetchone()[0]
            if active:
                raise TaskConflict('存在运行中的调用，不能让出检查点')
            connection.execute('UPDATE tasks SET status="queued",owner=NULL,epoch=epoch+1,lease_until=0,updated=? WHERE id=?', (now, task['id']))
            self._event(connection, task, 'task_yielded', now, status='queued')
            self._assert_commit_window(task)

    def fail(self, lease: Lease, attempt: Attempt, failure: StepFailure) -> None:
        """持久化已分类失败；仅可重试错误在次数、退避及总截止内进入retry_wait。"""
        with self._transaction() as connection:
            now = self._now()
            task = self._active_task(connection, lease, now)
            if task is None:
                return
            self._active_attempt(connection, lease, attempt)
            policy = self._step_policy(task, attempt.step_name).get('replay_policy', 'replay_safe')
            call = connection.execute('SELECT status FROM calls WHERE id=?', (attempt.call_id,)).fetchone()
            sent = failure.sent or (policy == 'at_most_once' and call['status'] == 'dispatched')
            self._settle(connection, attempt.call_id, failure.usage, sent=sent)
            connection.execute('UPDATE attempts SET status="failed",ended=?,error_code=? WHERE call_id=?', (now, failure.code, attempt.call_id))
            budget = Budget(**json.loads(task['budget_json']))
            delay = max(budget.retry_base_seconds * 2**(attempt.number - 1), failure.retry_after)
            retry = policy == 'replay_safe' and failure.code in RETRYABLE_CODES and attempt.number < budget.max_attempts and delay <= budget.max_retry_seconds and now + delay < task['deadline']
            self._event(connection, task, 'attempt_failed', now, status='failed', code=failure.code, step=attempt.step_name, attempt=attempt.number)
            if retry:
                connection.execute('UPDATE steps SET status="retry_wait",error_code=? WHERE task_id=? AND name=?', (failure.code, task['id'], attempt.step_name))
                connection.execute('UPDATE tasks SET status="retry_wait",reason=?,owner=NULL,lease_until=0,next_at=?,updated=? WHERE id=?', (failure.code, now + delay, now, task['id']))
                self._event(connection, task, 'retry_scheduled', now, status='retry_wait', code=failure.code)
            else:
                self._stop(connection, task, now, failure.code)
            self._assert_commit_window(task)

    def cancel(self, task_id: str) -> bool:
        """立即撤销提交权，保留已完成产物；终态不被取消操作覆写。"""
        with self._transaction() as connection:
            task, now = self._task(connection, task_id), self._now()
            if task['status'] in TERMINAL:
                return False
            if task['deadline'] <= now:
                grant = connection.execute('SELECT parent_task_id FROM child_grants WHERE task_id=?', (task['id'],)).fetchone()
                self._stop(connection, self._task(connection, grant['parent_task_id']) if grant else task, now, 'DEADLINE_EXCEEDED')
                return True
            self._stop(connection, task, now, 'CANCELLED', cancelled=True)
            return True

    def reconcile(self, call_id: str, usage: Usage, *, cost_unit: str | None = None) -> None:
        """以可信实际用量幂等对账；只对usage_pending开放，不能释放仍在途的调用。"""
        if not isinstance(usage, Usage):
            raise ValueError('无效实际用量')
        actual = canonical_json(asdict(usage)).decode()
        with self._transaction() as connection:
            call = connection.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
            if call is None:
                raise TaskConflict('调用不存在')
            task = self._task(connection, call['task_id'])
            attempt = connection.execute('SELECT step_name FROM attempts WHERE call_id=?', (call_id,)).fetchone()
            if attempt is None:
                raise TaskStorageError('调用尝试缺失')
            spec = self._step_policy(task, attempt['step_name'])
            grant = connection.execute('SELECT cost_unit FROM child_grants WHERE task_id=?', (call['task_id'],)).fetchone()
            expected_unit = grant['cost_unit'] if grant else spec.get('cost_unit')
            if (grant or spec.get('replay_policy', 'replay_safe') == 'at_most_once') and cost_unit != expected_unit:
                raise ValueError('对账成本单位不匹配')
            if call['status'] == 'settled' and call['actual_json'] == actual:
                return
            if call['status'] != 'usage_pending':
                raise TaskConflict('调用不能重复或提前结算')
            self._settle(connection, call_id, usage)
            self._event(connection, self._task(connection, call['task_id']), 'usage_reconciled', self._now())

    def payload(self, task_id: str) -> dict:
        """返回冻结操作输入；不输出到公共进度日志。"""
        with self._transaction(write=False) as connection:
            task = self._task(connection, task_id)
            body = task['input_json']
            if hashlib.sha256(body).hexdigest() != task['input_hash']:
                raise TaskStorageError('任务输入摘要不一致')
            return json.loads(body)

    def output(self, task_id: str, step_name: str | None = None) -> dict | None:
        """读取已完成步骤的不可变产物并校验摘要；没有产物返回None。"""
        with self._transaction(write=False) as connection:
            self._task(connection, task_id)
            query = 'SELECT output_hash FROM steps WHERE task_id=? AND status="completed"'
            args = [task_id]
            if step_name is not None:
                query += ' AND name=?'
                args.append(step_name)
            row = connection.execute(query + ' ORDER BY ordinal DESC LIMIT 1', args).fetchone()
            if row is None:
                return None
            artifact = connection.execute('SELECT body FROM artifacts WHERE hash=?', (row['output_hash'],)).fetchone()
            if artifact is None or hashlib.sha256(artifact['body']).hexdigest() != row['output_hash']:
                raise TaskStorageError('任务产物缺失或摘要不一致')
            return json.loads(artifact['body'])

    def inspect(self, task_id: str) -> dict:
        """读取同一事务快照中的进度、历史、预算和错误码，不展开输入/源文或异常消息。"""
        with self._transaction(write=False) as connection:
            task = self._task(connection, task_id)
            result = {key: task[key] for key in ('id', 'run_id', 'request_id', 'parent_id', 'status', 'reason', 'created', 'updated', 'deadline', 'epoch', 'lease_until', 'next_at', 'input_hash')}
            result['steps'] = [dict(row) for row in connection.execute('SELECT name,ordinal,handler,version,status,attempts,output_hash,partial,error_code FROM steps WHERE task_id=? ORDER BY ordinal', (task_id,))]
            result['attempts'] = [dict(row) for row in connection.execute('SELECT step_name,number,call_id,epoch,status,started,ended,error_code FROM attempts WHERE task_id=? ORDER BY started,number', (task_id,))]
            result['calls'] = [dict(row) for row in connection.execute('SELECT id,status,estimate_json,actual_json FROM calls WHERE task_id=?', (task_id,))]
            result['events'] = [dict(row) for row in connection.execute('SELECT * FROM events WHERE task_id=? ORDER BY id', (task_id,))]
            if task['status'] == 'waiting_input':
                children = connection.execute('SELECT t.status FROM child_grants g JOIN tasks t ON t.id=g.task_id WHERE g.parent_task_id=?', (task_id,)).fetchall()
                if children:
                    result['waitReason'] = 'children_pending'
                    result['pendingChildren'] = sum(row['status'] not in TERMINAL for row in children)
            used, held, pending = self._charges(connection, task['run_id'])
            limits = json.loads(task['budget_json'])
            result['budget'] = {'limits': limits, 'used': used, 'held': held, 'usagePending': pending,
                                'overrun': _exceeds(limits, used, held)}
            grant = connection.execute('SELECT grant_json,cost_unit,parent_task_id FROM child_grants WHERE task_id=?', (task_id,)).fetchone()
            if grant:
                own_used, own_held, _ = self._charges(connection, task['run_id'], task_id)
                grant_value = json.loads(grant['grant_json'])
                result['childQuota'] = {'grant': grant_value, 'costUnit': grant['cost_unit'],
                                        'parentTaskId': grant['parent_task_id'], 'used': own_used, 'held': own_held,
                                        'overrun': _exceeds(grant_value, own_used, own_held)}
            elif connection.execute('SELECT 1 FROM child_grants WHERE run_id=?', (task['run_id'],)).fetchone():
                result['budget']['allocated'] = self._unspent_grants(connection, task['run_id'])
                overruns = []
                for row in connection.execute('SELECT task_id,grant_json FROM child_grants WHERE parent_task_id=?', (task_id,)):
                    own_used, own_held, _ = self._charges(connection, task['run_id'], row['task_id'])
                    grant_value = json.loads(row['grant_json'])
                    if _exceeds(grant_value, own_used, own_held):
                        overruns.append(row['task_id'])
                result['budget']['childOverruns'] = overruns
            return result

    def list_tasks(self, *, limit: int = 50) -> list[dict]:
        """列出最近的任务概要；不读取输入与产物正文。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('无效查询上限')
        with self._transaction(write=False) as connection:
            return [dict(row) for row in connection.execute('SELECT id,run_id,status,reason,created,updated FROM tasks ORDER BY created DESC,id LIMIT ?', (limit,))]
