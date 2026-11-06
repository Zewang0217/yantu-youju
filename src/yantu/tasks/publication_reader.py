"""只读打开已有任务数据库；不初始化、迁移或修复用户文件。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import TaskStorageError
from .publications import TaskPublicationRepository

_REQUIRED = {
    'runs': {'id', 'request_id', 'budget_json', 'deadline'},
    'tasks': {'id', 'run_id', 'input_hash', 'plan_json', 'status', 'epoch'},
    'steps': {'task_id', 'name', 'handler', 'version'},
    'calls': {'id', 'run_id', 'task_id', 'status'},
    'events': {'id', 'run_id', 'task_id', 'event'},
    'publication_versions': {'version', 'oid', 'body'},
    'publication_current': {'oid', 'version'},
}


class ReadOnlyTaskStore:
    """仅提供发布读取所需的事务，不运行 TaskStore 的建表流程。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise TaskStorageError('任务数据库不存在')

    @contextmanager
    def _transaction(self, *, write=False):
        if write:
            raise TaskStorageError('只读发布库不能写入')
        connection = None
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                         timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise TaskStorageError('不是受支持版本的任务数据库')
            for table, expected in _REQUIRED.items():
                columns = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
                if not expected.issubset(columns):
                    raise TaskStorageError('任务数据库结构不完整')
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            raise TaskStorageError('只读发布库不可用') from error
        finally:
            if connection is not None:
                connection.close()

    def validate(self):
        with self._transaction(write=False):
            pass


def open_publication_reader(path, snapshots):
    """打开已存在的任务库并校验结构；返回仅允许读取的发布仓储。"""
    store = ReadOnlyTaskStore(path)
    store.validate()
    return TaskPublicationRepository(store, snapshots)
