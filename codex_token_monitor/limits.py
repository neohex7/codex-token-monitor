"""Optional read-only RPC client. Only initialize and account/rateLimits/read are sent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import threading
import time
from .telemetry import normalize_limit


def find_codex_binary(explicit=None):
    """Resolve a user override, PATH command, or standard desktop installation."""
    override = explicit or os.environ.get('CODEX_MONITOR_CODEX_BIN')
    if override:
        expanded = os.path.expanduser(override)
        command = shutil.which(expanded)
        return command if command else None
    command = shutil.which('codex')
    if command:
        return command
    candidates = (
        Path('/usr/lib/chatgpt/resources/codex'),
        Path('/opt/ChatGPT/resources/codex'),
        Path('/Applications/ChatGPT.app/Contents/Resources/codex'),
        Path.home() / 'Applications/ChatGPT.app/Contents/Resources/codex',
    )
    return next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


class LimitsClient:
    def __init__(self, codex_home, interval=5.0, binary=None):
        self.home = Path(codex_home)
        self.binary = find_codex_binary(binary)
        self.interval = max(5.0, interval)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.limits = []
        self.error = None
        self.process = None
        self.thread = threading.Thread(target=self._worker, name='codex-limits-readonly', daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return list(self.limits), self.error

    def _worker(self):
        while not self.stop.is_set():
            try:
                self._connection()
            except (OSError, ValueError, RuntimeError, BrokenPipeError):
                with self.lock:
                    self.error = 'Live quota read unavailable; showing timestamped rollout/last-known quota.'
                self.ready.set()
            finally:
                self._terminate()
            self.stop.wait(30)

    def _terminate(self):
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process:
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()
        self.process = None

    def _connection(self):
        if not self.binary:
            raise RuntimeError('Codex binary unavailable')
        env = os.environ.copy()
        env['CODEX_HOME'] = str(self.home)
        process = subprocess.Popen([self.binary, 'app-server', '--listen', 'stdio://',
                                    '-c', 'analytics.enabled=false'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.process = process
        def send(message):
            process.stdin.write((json.dumps(message)+'\n').encode())
            process.stdin.flush()
        send({'id':1, 'method':'initialize', 'params':{
            'clientInfo':{'name':'codex_token_monitor','title':'Codex Token Monitor','version':'0.1.0'},
            'capabilities':{'experimentalApi':True}}})
        buffer = b''
        initialized = False
        request_id = 1
        pending = 1
        deadline = time.monotonic()+15
        next_read = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not self.stop.is_set():
                now = time.monotonic()
                if pending is not None and now > deadline:
                    raise RuntimeError('RPC timeout')
                if initialized and pending is None and now >= next_read:
                    request_id += 1
                    pending = request_id
                    send({'id':request_id,'method':'account/rateLimits/read'})
                    deadline = now+15
                if not selector.select(.2):
                    continue
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError('RPC closed')
                buffer += data
                if len(buffer) > 4*1024*1024:
                    raise RuntimeError('Oversized RPC data')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    if pending is None or message.get('id') != pending:
                        continue
                    if 'error' in message:
                        raise RuntimeError('RPC rejected')
                    if not initialized:
                        initialized = True
                        pending = None
                        send({'method':'initialized'})
                        continue
                    result = message.get('result') or {}
                    buckets = result.get('rateLimitsByLimitId')
                    if not buckets and result.get('rateLimits'):
                        buckets = {'codex':result['rateLimits']}
                    values = [normalize_limit(raw, time.time(), 'account/rateLimits/read')
                              for raw in (buckets or {}).values() if isinstance(raw,dict)]
                    with self.lock:
                        self.limits = values
                        self.error = None if values else 'Quota endpoint returned no quota data.'
                    self.ready.set()
                    pending = None
                    next_read = time.monotonic()+self.interval

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)
