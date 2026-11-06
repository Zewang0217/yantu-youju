"""创建、推进、查询或取消离线资料任务；不启动后台调度或正式政策发布。"""

import argparse
import json
import logging
from pathlib import Path

from yantu.acquisition.artifact_files import atomic_write, canonical_json
from yantu.acquisition.error_events import log_failure
from yantu.acquisition.policy_applicability import ApplicabilityTarget
from yantu.tasks.models import Budget
from yantu.tasks.policy_workflow import policy_registry, submit_policy_task
from yantu.tasks.runner import TaskRunner
from yantu.tasks.store import TaskStore


def main() -> None:
    """执行显式管理命令；重试等待直接返回，下次resume再推进。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('var/task_runs/tasks.sqlite3'))
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit')
    submit.add_argument('bundle', type=Path)
    submit.add_argument('--key', required=True)
    submit.add_argument('--target', type=Path)
    submit.add_argument('--calls', type=int, default=10)
    submit.add_argument('--seconds', type=float, default=300)
    submit.add_argument('--attempts', type=int, default=3)
    submit.add_argument('--parent-task')
    commands.add_parser('list')
    for name in ('resume', 'show', 'cancel', 'export'):
        sub = commands.add_parser(name)
        sub.add_argument('task')
        if name == 'resume':
            sub.add_argument('--steps', type=int, default=32)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    store = TaskStore(args.db)
    if args.command == 'submit':
        target = ApplicabilityTarget(**json.loads(args.target.read_text(encoding='utf-8'))) if args.target else None
        task_id = submit_policy_task(store, args.bundle, idempotency_key=args.key,
            budget=Budget(tool_calls=args.calls, wall_seconds=args.seconds, max_attempts=args.attempts),
            target=target, parent_task_id=args.parent_task)
        print(json.dumps({'taskId': task_id, 'status': store.inspect(task_id)['status']}, ensure_ascii=False))
    elif args.command == 'list':
        print(json.dumps(store.list_tasks(), ensure_ascii=False))
    elif args.command == 'show':
        print(json.dumps(store.inspect(args.task), ensure_ascii=False))
    elif args.command == 'cancel':
        print(json.dumps({'cancelled': store.cancel(args.task), 'status': store.inspect(args.task)['status']}))
    elif args.command == 'export':
        value = store.output(args.task, 'applicability')
        if value is None:
            raise ValueError('任务尚无已提交产物')
        output = args.db.parent / 'exports' / f'{args.task}.json'
        atomic_write(output, canonical_json(value))
        print(json.dumps({'output': str(output.resolve())}, ensure_ascii=False))
    else:
        result = TaskRunner(store, policy_registry()).run(args.task, max_steps=args.steps)
        print(json.dumps({key: result[key] for key in ('id', 'run_id', 'status', 'reason', 'steps', 'budget')}, ensure_ascii=False))
        if result['status'] == 'failed':
            raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        log_failure(logging.getLogger(__name__), error, event='task_cli_failed')
        raise SystemExit(1) from None
