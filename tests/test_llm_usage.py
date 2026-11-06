"""离线验证供应商计量与错误分类；不发起外部请求。"""

import io
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import Message

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from yantu.llm.client import ChatClient, LLMError, _retry_after
from yantu.llm.task_adapter import MeasuredCost, chat_result_to_step
from yantu.tasks.models import Budget, StepSpec, Usage
from yantu.tasks.store import TaskStore


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def __call__(self, req, timeout):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return io.BytesIO(json.dumps(self.response).encode())


def client(response):
    transport = Transport(response)
    return ChatClient('https://example.invalid/secret-url', 'secret-key', 'test-model', transport=transport), transport


def body(usage):
    return {'choices': [{'message': {'content': 'ok'}}], 'usage': usage}


class UsageTests(unittest.TestCase):
    def test_known_token_unknown_cost_retains_meter_and_reservation(self):
        c, transport = client(body({'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}))
        result = c.chat_with_usage([])
        step = chat_result_to_step(result)
        self.assertIsNone(step.usage)
        self.assertEqual(step.value['providerUsage']['total_tokens'], 10)
        self.assertEqual(step.value['providerUsage']['input_tokens'], 7)
        self.assertEqual(step.value['providerUsage']['total_origin'], 'provider')
        self.assertIsNone(step.value['measuredCost'])
        self.assertEqual(transport.calls, 1)
        json.dumps(step.value)

    def test_complete_meter_and_trusted_cost(self):
        c, _ = client(body({'prompt_tokens': 7, 'completion_tokens': 3}))
        result = c.chat_with_usage([])
        self.assertEqual(result.usage.total_origin, 'derived')
        cost = MeasuredCost(4, 'credits', 'billing_statement')
        step = chat_result_to_step(result, cost=cost, budget_unit='credits')
        self.assertEqual(step.usage, Usage(tool_calls=1, tokens=10, cost_units=4))
        self.assertEqual(step.value['measuredCost']['source'], 'billing_statement')
        with self.assertRaises(ValueError):
            chat_result_to_step(result, cost=cost, budget_unit='other')

    def test_partial_meter_is_persisted_with_held_budget(self):
        c, _ = client(body({'prompt_tokens': 7, 'completion_tokens': 3}))
        result = chat_result_to_step(c.chat_with_usage([]))
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(pathlib.Path(directory) / 'tasks.sqlite3')
            task = store.submit(request_id='llm-test', idempotency_key='llm-key', payload={},
                plan=(StepSpec('model', 'model-test', 'v1', Usage(1, 20, 5)),),
                budget=Budget(tool_calls=1, tokens=20, cost_units=5))
            lease = store.claim(task, owner='worker')
            attempt = store.start_step(lease)
            store.dispatched(lease, attempt)
            store.finish(lease, attempt, result)
            snapshot = store.inspect(task)
            self.assertEqual(snapshot['budget']['usagePending'], 1)
            self.assertEqual(snapshot['budget']['held'], {'tool_calls': 1, 'tokens': 20, 'cost_units': 5})
            self.assertEqual(store.output(task)['providerUsage']['total_tokens'], 10)

    def test_missing_invalid_and_inconsistent_meter_never_settles(self):
        cases = [(None, 'missing'), ({'prompt_tokens': 5}, 'partial'),
                 ({'prompt_tokens': 5, 'completion_tokens': True}, 'invalid'),
                 ({'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 8}, 'inconsistent')]
        cost = MeasuredCost(0, 'credits', 'billing_statement')
        for meter, status in cases:
            with self.subTest(meter=meter):
                c, _ = client(body(meter))
                result = c.chat_with_usage([])
                self.assertEqual(result.usage.status, status)
                self.assertIsNone(chat_result_to_step(result, cost=cost, budget_unit='credits').usage)
                if status == 'invalid':
                    self.assertEqual(result.usage.input_tokens, 5)

    def test_zero_is_known_only_when_explicit(self):
        c, _ = client(body({'total_tokens': 0}))
        result = c.chat_with_usage([])
        self.assertIsNone(chat_result_to_step(result).usage)
        self.assertEqual(chat_result_to_step(result, cost=MeasuredCost(0, 'credits', 'billing_statement'), budget_unit='credits').usage.tokens, 0)

    def test_partial_component_must_not_exceed_provider_total(self):
        cost = MeasuredCost(1, 'credits', 'billing_statement')
        for field in ('prompt_tokens', 'completion_tokens'):
            for component, total, expected in ((100, 1, 'inconsistent'), (1, 100, 'complete'), (100, 100, 'complete')):
                with self.subTest(field=field, component=component, total=total):
                    c, _ = client(body({field: component, 'total_tokens': total}))
                    result = c.chat_with_usage([])
                    self.assertEqual(result.usage.status, expected)
                    step = chat_result_to_step(result, cost=cost, budget_unit='credits')
                    if expected == 'inconsistent':
                        self.assertIsNone(step.usage)
                        self.assertEqual(step.value['providerUsage']['total_tokens'], 1)
                    else:
                        self.assertEqual(step.usage.tokens, total)

    def test_chat_compatibility_and_credential_repr(self):
        c, transport = client(body(None))
        self.assertEqual(c.chat([]), 'ok')
        self.assertEqual(transport.calls, 1)
        self.assertNotIn('secret', repr(c))

    def test_bad_response_retains_meter_and_does_not_leak(self):
        c, _ = client({'usage': {'total_tokens': 10}, 'error': 'secret-response'})
        with self.assertRaises(LLMError) as raised:
            c.chat_with_usage([])
        self.assertEqual(raised.exception.usage.total_tokens, 10)
        self.assertNotIn('secret-response', str(raised.exception))

    def test_http_errors_never_read_body_or_retry(self):
        class SensitiveBody(io.BytesIO):
            read_called = False
            def read(self, *args):
                self.read_called = True
                return super().read(*args)
        for status, expected in [(429, 'RATE_LIMITED'), (401, 'AUTH_REQUIRED'), (403, 'FORBIDDEN')]:
            with self.subTest(status=status):
                headers = Message()
                headers['Retry-After'] = '17'
                sensitive = SensitiveBody(b'secret-body')
                error = urllib.error.HTTPError('https://invalid/?secret-url', status, 'secret-reason', headers, sensitive)
                c, transport = client(error)
                with self.assertRaises(LLMError) as raised:
                    c.chat_with_usage([])
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn('secret', str(raised.exception))
                self.assertFalse(sensitive.read_called)
                self.assertEqual(transport.calls, 1)
                if status == 429:
                    self.assertEqual(raised.exception.retry_after, 17)
                    self.assertTrue(raised.exception.retry_after_present)

    def test_timeout_is_unknown_and_sanitized(self):
        c, transport = client(TimeoutError('secret-timeout'))
        with self.assertRaises(LLMError) as raised:
            c.chat_with_usage([])
        self.assertEqual(raised.exception.code, 'NETWORK_TIMEOUT')
        self.assertTrue(raised.exception.sent)
        self.assertNotIn('secret-timeout', str(raised.exception))
        self.assertEqual(transport.calls, 1)

    def test_retry_after_date_missing_and_invalid(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(_retry_after({'Retry-After': 'Thu, 01 Jan 2026 00:00:30 GMT'}, now), (30, True))
        self.assertEqual(_retry_after({}, now), (None, False))
        self.assertEqual(_retry_after({'Retry-After': 'not-a-date'}, now), (None, True))


if __name__ == '__main__':
    unittest.main()
