import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import aiohttp
from autocanvas import live_gemini
from autocanvas.live_gemini import GeminiLiveMonitor
from autocanvas.types import AudioChunk, MediaSource, RemoteError


class FakeWS:
    def __init__(self, frames):
        self.closed = False
        self.sent = []
        self.queue = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(frame)

    async def send_json(self, obj):
        self.sent.append(obj)

    async def receive(self):
        frame = await self.queue.get()
        return type('Msg', (), {'type': aiohttp.WSMsgType.TEXT, 'data': json.dumps(frame)})()

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, ws=None, connect_error=None):
        self.ws = ws
        self.connect_error = connect_error
        self.closed = False
        self.connects = []
        self.proxies = []

    async def ws_connect(self, url, headers=None, proxy=None):
        self.connects.append((url, headers))
        self.proxies.append(proxy)
        if self.connect_error:
            raise self.connect_error
        return self.ws

    async def close(self):
        self.closed = True


async def resolve(lecture):
    return [MediaSource('fake')]


async def select(sources, *args):
    return sources[0]


async def short_reader(source, **kw):
    for i in range(3):
        yield AudioChunk(b'\0\0'*160, 16000, i*0.01)
        await asyncio.sleep(0.001)


class GeminiLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_interim_ignored_final_kept_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = [
                {'setupComplete': {}},
                {'serverContent': {'interimInputTranscription': {'text': '今天我'}}},
                {'serverContent': {'interimInputTranscription': {'text': '今天我们讲'}}},
                {'serverContent': {'inputTranscription': {'text': '今天我们讲第三章'}}},
            ]
            ws = FakeWS(frames)
            session = FakeSession(ws=ws)
            with patch.object(live_gemini.aiohttp, 'ClientSession', lambda *a, **k: session):
                monitor = GeminiLiveMonitor(resolve, api_key='x', model='m', reader=short_reader, selector=select, reconnect_seconds=.01)
                now = datetime.now(timezone.utc)
                lecture = {'begin': now.isoformat(), 'end': (now+timedelta(seconds=.15)).isoformat()}
                path = await monitor.run(lecture, Path(tmp))
            rows = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual([r['text'] for r in rows], ['今天我们讲第三章'])
            self.assertEqual(session.connects, [(live_gemini.LIVE_ENDPOINT, {'x-goog-api-key': 'x'})])
            self.assertEqual(ws.sent[0], {'setup': {'model': 'models/m', 'generationConfig': {'responseModalities': ['TEXT']},
                                                    'inputAudioTranscription': {'languageCodes': ['cmn-Hans-CN'], 'mode': 'VERBATIM'}}})

    async def test_proxy_only_when_set(self):
        for proxy, expected in (('http://127.0.0.1:3128', 'http://127.0.0.1:3128'), ('', None), (None, None)):
            with tempfile.TemporaryDirectory() as tmp:
                session = FakeSession(ws=FakeWS([{'setupComplete': {}}]))
                with patch.object(live_gemini.aiohttp, 'ClientSession', lambda *a, **k: session):
                    monitor = GeminiLiveMonitor(resolve, api_key='x', model='m', reader=short_reader, selector=select, reconnect_seconds=.01, proxy=proxy)
                    now = datetime.now(timezone.utc)
                    await monitor.run({'begin': now.isoformat(), 'end': (now+timedelta(seconds=.15)).isoformat()}, Path(tmp))
                self.assertEqual(session.proxies[:1], [expected])

    async def test_keyword_fires_on_final_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = [
                {'setupComplete': {}},
                {'serverContent': {'interimInputTranscription': {'text': '现在签到'}}},
                {'serverContent': {'inputTranscription': {'text': '现在签到吧'}}},
            ]
            ws = FakeWS(frames)
            session = FakeSession(ws=ws)
            with patch.object(live_gemini.aiohttp, 'ClientSession', lambda *a, **k: session):
                monitor = GeminiLiveMonitor(resolve, api_key='x', model='m', reader=short_reader, selector=select,
                                            reconnect_seconds=.01, keywords=['签到'])
                now = datetime.now(timezone.utc)
                lecture = {'begin': now.isoformat(), 'end': (now+timedelta(seconds=.15)).isoformat()}
                path = await monitor.run(lecture, Path(tmp))
            events = [json.loads(l) for l in (Path(tmp)/'events.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sum(e['type'] == 'keyword' for e in events), 1)

    async def test_socket_error_raises_remote_error_not_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = FakeSession(connect_error=ConnectionError('boom'))

            async def infinite_reader(source, **kw):
                while True:
                    yield AudioChunk(b'\0\0'*160, 16000, 0)
                    await asyncio.sleep(1)

            with patch.object(live_gemini.aiohttp, 'ClientSession', lambda *a, **k: session):
                monitor = GeminiLiveMonitor(resolve, api_key='x', model='m', reader=infinite_reader, selector=select, reconnect_seconds=.01)
                now = datetime.now(timezone.utc)
                lecture = {'begin': now.isoformat(), 'end': (now+timedelta(hours=1)).isoformat()}
                with self.assertRaises(RemoteError):
                    await monitor.run(lecture, Path(tmp))

    async def test_producer_cleanup_on_cancel_leaves_no_pending_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            closed = asyncio.Event()

            async def infinite_reader(source, **kw):
                try:
                    while True:
                        yield AudioChunk(b'00'*16, 16000, 0)
                        await asyncio.sleep(.01)
                finally:
                    closed.set()

            ws = FakeWS([{'setupComplete': {}}])
            session = FakeSession(ws=ws)
            with patch.object(live_gemini.aiohttp, 'ClientSession', lambda *a, **k: session):
                monitor = GeminiLiveMonitor(resolve, api_key='x', model='m', reader=infinite_reader, selector=select, reconnect_seconds=.01)
                now = datetime.now(timezone.utc)
                lecture = {'begin': now.isoformat(), 'end': (now+timedelta(hours=1)).isoformat()}
                before = asyncio.all_tasks() - {asyncio.current_task()}
                task = asyncio.create_task(monitor.run(lecture, Path(tmp)))
                await asyncio.sleep(.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(closed.is_set())
                await asyncio.sleep(0)
                remaining = (asyncio.all_tasks() - {asyncio.current_task()}) - before
                self.assertEqual(remaining, set())


if __name__ == '__main__':
    unittest.main()
