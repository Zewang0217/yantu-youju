"""冻结官方快照在两种仓储的实际演示读侧保持相同业务判断。"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import trusted_policy_demo


class PublicationDemoTests(unittest.TestCase):
    def test_sqlite_task_and_file_reader_produce_same_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'data' / 'pilot' / 'trusted_policy'
            fixture.parent.mkdir(parents=True)
            shutil.copytree(trusted_policy_demo.ROOT / 'data' / 'pilot' / 'trusted_policy', fixture)
            results = []
            for backend in ('file', 'sqlite'):
                output = io.StringIO()
                with patch.object(trusted_policy_demo, 'ROOT', root), \
                     patch.object(sys, 'argv', ['trusted_policy_demo.py', '--store', backend]), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(trusted_policy_demo.main(), 0)
                result, _ = json.JSONDecoder().raw_decode(output.getvalue())
                self.assertEqual(result['store'], backend)
                self.assertEqual(result['evidenceFailures'], [])
                results.append(result)
            for key in ('publicationStatus', 'eligibility', 'opening', 'included', 'unknownPoints', 'evidence'):
                self.assertEqual(results[0][key], results[1][key], key)

    def test_sqlite_rejects_llm_before_loading_credentials(self):
        with patch.object(sys, 'argv', ['trusted_policy_demo.py', '--store', 'sqlite', '--llm']), \
             patch.object(trusted_policy_demo, 'load_client') as load_client:
            with self.assertRaises(SystemExit):
                trusted_policy_demo.main()
            load_client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
