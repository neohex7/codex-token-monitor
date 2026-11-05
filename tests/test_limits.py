import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from codex_token_monitor.limits import LimitsClient, find_codex_binary


class LimitsTests(unittest.TestCase):
    def test_explicit_binary_takes_precedence_over_environment(self):
        with patch.dict(os.environ, {'CODEX_MONITOR_CODEX_BIN':'environment-codex'}), patch('codex_token_monitor.limits.shutil.which',return_value='/example/codex') as which:
            self.assertEqual(find_codex_binary('explicit-codex'),'/example/codex')
            which.assert_called_once_with('explicit-codex')

    def test_invalid_override_does_not_fall_back_to_another_installation(self):
        with patch.dict(os.environ, {'CODEX_MONITOR_CODEX_BIN':'missing-codex'}), patch('codex_token_monitor.limits.shutil.which',return_value=None) as which:
            self.assertIsNone(find_codex_binary())
            which.assert_called_once_with('missing-codex')

    def test_path_command_precedes_desktop_fallback(self):
        with patch.dict(os.environ, {'CODEX_MONITOR_CODEX_BIN':''}), patch('codex_token_monitor.limits.shutil.which',return_value='/example/bin/codex') as which:
            self.assertEqual(find_codex_binary(),'/example/bin/codex')
            which.assert_called_once_with('codex')

    def test_notifications_ignored_and_only_readonly_rpc_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            audit=root/'audit.jsonl'
            binary=root/'fake-codex'
            binary.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
audit=Path(__file__).with_name('audit.jsonl')
for line in sys.stdin:
    message=json.loads(line)
    with audit.open('a') as out:out.write(json.dumps(message)+'\\n')
    method=message.get('method')
    if method=='initialize':
        print(json.dumps({'id':message['id'],'result':{'userAgent':'fake'}}),flush=True)
        print(json.dumps({'method':'account/updated','params':{}}),flush=True)
    elif method=='account/rateLimits/read':
        print(json.dumps({'id':message['id'],'result':{'rateLimitsByLimitId':{
            'codex':{'limitId':'codex','primary':{'usedPercent':12.345,'windowDurationMins':10080,'resetsAt':9999999999}},
            'other':{'limitId':'other','primary':{'usedPercent':7,'windowDurationMins':300,'resetsAt':9999999999}}
        }}}),flush=True)
''')
            binary.chmod(0o700)
            client=LimitsClient(root,binary=str(binary))
            try:
                self.assertTrue(client.ready.wait(timeout=3))
                limits,error=client.snapshot()
                self.assertIsNone(error)
                self.assertEqual(len(limits),2)
                self.assertEqual(limits[0]['primary']['used_percent'],12.345)
                self.assertEqual(limits[0]['source'],'account/rateLimits/read')
                process=client.process
            finally:
                client.close()
            self.assertFalse(client.thread.is_alive())
            self.assertIsNotNone(process.poll())
            methods=[json.loads(l)['method'] for l in audit.read_text().splitlines()]
            self.assertEqual(methods,['initialize','initialized','account/rateLimits/read'])


if __name__=='__main__':unittest.main()
