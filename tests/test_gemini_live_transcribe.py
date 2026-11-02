import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from autocanvas import gemini_live_transcribe as live
from autocanvas.gemini_live_transcribe import GeminiLiveFileTranscriber
from autocanvas.types import AudioChunk, AuthenticationRequired, RateLimited, RemoteError

KEY = 'test-secret-key'
real_sleep = asyncio.sleep


def vad(kind, at):
    return {'voiceActivity': {'type': kind, 'audioOffset': f'{at}s'}}


def final(text):
    return {'serverContent': {'inputTranscription': {'text': text}}}


def close(code, reason=''):
    return SimpleNamespace(type=aiohttp.WSMsgType.CLOSE, data=code, extra=reason)


TURN = {'serverContent': {'turnComplete': True}}


class FakeWS:
    """Script items fire once `trigger` audio frames were sent ('end': after audioStreamEnd); a frame send waits until the receiver handled them."""
    def __init__(self, script=()):
        self.script, self.sent, self.closed, self.busy = [(0, {'setupComplete': {}}), *script], [], False, False

    audio = property(lambda self: sum('audio' in m.get('realtimeInput', {}) for m in self.sent))
    ended = property(lambda self: any(m.get('realtimeInput', {}).get('audioStreamEnd') for m in self.sent))

    def ready(self):
        return bool(self.script) and (self.ended if self.script[0][0] == 'end' else self.audio >= self.script[0][0])

    async def send_json(self, obj):
        self.sent.append(obj)
        while 'audio' in obj.get('realtimeInput', {}) and (self.ready() or self.busy):
            await real_sleep(0)

    async def receive(self):
        self.busy = False
        while not self.ready():
            await real_sleep(0.001)
        self.busy = True
        item = self.script.pop(0)[1]
        return item if isinstance(item, SimpleNamespace) else SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(item), extra=None)

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, ws=None, error=None):
        self.ws, self.error, self.connects, self.proxies, self.closed = ws, error, [], [], False

    async def ws_connect(self, url, headers=None, proxy=None):
        self.connects.append((url, headers))
        self.proxies.append(proxy)
        if self.error:
            raise self.error
        return self.ws

    async def close(self):
        self.closed = True


class Transcriber(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.waits, self.reads, self.clock = [], [], [0.0]
        async def sleep(seconds):
            self.waits.append(round(seconds, 6))
            self.clock[0] += seconds
            await real_sleep(0)
        for target in (patch('autocanvas.gemini_live_transcribe.asyncio.sleep', sleep),
                       patch('autocanvas.gemini_live_transcribe.time', SimpleNamespace(monotonic=lambda: self.clock[0]))):
            target.start()
            self.addCleanup(target.stop)

    def reader(self, seconds):
        async def read(source, *, chunk_seconds):
            self.reads.append((source.location, chunk_seconds))
            try:
                for i in range(round(seconds/chunk_seconds)):
                    yield AudioChunk(b'\0\0'*1600, 16000, i*0.1)
            finally:
                self.reads.append('closed')
        return read

    async def run_live(self, script, seconds=1.0, offset=0, duration=None, speed=4, session=None, proxy=None):
        self.ws = FakeWS(script)
        self.session = session or FakeSession(self.ws)
        t = GeminiLiveFileTranscriber(KEY, speed=speed, idle=0.05, reader=self.reader(seconds), proxy=proxy)
        with patch.object(live.aiohttp, 'ClientSession', lambda: self.session):
            rows = await t.transcribe('chunk.mp3', offset, seconds if duration is None else duration)
        return [(round(r.start, 3), round(r.end, 3), r.text) for r in rows]

    async def test_proxy_only_when_set(self):
        await self.run_live([(10, final('你好')), ('end', TURN)], proxy='http://127.0.0.1:3128')
        self.assertEqual(self.session.proxies, ['http://127.0.0.1:3128'])
        for empty in (None, ''):
            await self.run_live([(10, final('你好')), ('end', TURN)], proxy=empty)
            self.assertEqual(self.session.proxies, [None])

    async def test_pacing_setup_and_single_stream_end(self):
        rows = await self.run_live([(10, final('你好')), ('end', TURN)])
        self.assertEqual(self.waits, [0.025]*10)
        self.assertEqual(self.ws.audio, 10)
        self.assertEqual(self.reads, [('chunk.mp3', 0.1), 'closed'])
        self.assertEqual(self.session.connects, [(live.ENDPOINT, {'x-goog-api-key': KEY})])
        self.assertNotIn(KEY, json.dumps(self.ws.sent))
        self.assertEqual(self.ws.sent[0], {'setup': {'model': 'models/gemini-3.5-transcribe-live', 'generationConfig': {'responseModalities': ['TEXT']},
                                                     'inputAudioTranscription': {'languageCodes': ['cmn-Hans-CN'], 'mode': 'VERBATIM'}}})
        self.assertEqual(self.ws.sent[1]['realtimeInput']['audio']['mimeType'], 'audio/pcm;rate=16000')
        ends = [i for i, m in enumerate(self.ws.sent) if m.get('realtimeInput', {}).get('audioStreamEnd')]
        self.assertEqual(ends, [len(self.ws.sent)-1])
        self.assertTrue(self.ws.closed and self.session.closed)
        self.assertEqual(rows, [(0, 1.0, '你好')])
        self.assertEqual(GeminiLiveFileTranscriber(KEY, model='models/x').model, 'models/x')

    async def test_pacing_follows_deadline_after_slow_sleeps(self):
        async def slow(seconds):
            self.waits.append(round(seconds, 6))
            self.clock[0] += seconds+{1: 0.015, 2: 3.0}.get(len(self.waits), 0)
            await real_sleep(0)
        with patch('autocanvas.gemini_live_transcribe.asyncio.sleep', slow):
            await self.run_live([('end', TURN)], seconds=6.0)
        # An overshoot is taken from the next wait; after a 3 s stall only 1 s (40 frames at 4x) is caught up.
        self.assertEqual(self.waits, [0.025, 0.01]+[0]*41+[0.025]*17)

    async def test_timing_from_voice_activity(self):
        script = [(5, vad('ACTIVITY_START', 0.2)), (10, vad('ACTIVITY_END', 0.9)), (12, vad('ACTIVITY_START', 1.1)),
                  (13, {'serverContent': {'interimInputTranscription': {'text': '甲'}}}), (15, vad('ACTIVITY_END', 1.4)), (16, final(' 甲乙 ')),
                  (18, vad('ACTIVITY_START', 1.6)), (22, final('丙')), (24, vad('ACTIVITY_END', 2.3)),
                  ('end', final('丁')), ('end', TURN)]
        rows = await self.run_live(script, seconds=3.0, offset=100)
        # Two windows before one final; a final inside an open window, corrected by its end; no window left uses the sent position.
        self.assertEqual(rows, [(100.2, 101.4, '甲乙'), (101.6, 102.3, '丙'), (102.3, 103.0, '丁')])

    async def test_no_vad_fallback_and_clamp(self):
        rows = await self.run_live([(5, final('一')), (12, final('二')), ('end', final('三'))], seconds=2.0, offset=10, duration=1.5)
        # No turnComplete: the idle timeout ends the session after audioStreamEnd.
        self.assertEqual(rows, [(10, 10.5, '一'), (10.5, 11.2, '二'), (11.2, 11.5, '三')])

    async def test_abnormal_close_before_end_is_remote_error(self):
        with self.assertRaises(RemoteError) as caught:
            await self.run_live([(3, final('部分')), (5, close(1011, 'Internal error'))], seconds=2.0)
        self.assertNotIsInstance(caught.exception, (RateLimited, AuthenticationRequired))
        self.assertEqual((caught.exception.stage, caught.exception.code), ('gemini_live', 1011))
        self.assertFalse(self.ws.ended)
        self.assertTrue(self.ws.closed and self.session.closed and self.reads[-1] == 'closed')
        self.assertNotIn(KEY, str(caught.exception))

    async def test_close_after_end_keeps_results(self):
        rows = await self.run_live([(10, final('完')), ('end', close(1011))])
        self.assertEqual(rows, [(0, 1.0, '完')])

    async def test_quota_signals_are_rate_limited(self):
        for script in ([(2, close(1008, 'RESOURCE_EXHAUSTED: quota exceeded'))], [('end', close(1011, 'You exceeded your current quota'))]):
            with self.subTest(script=script), self.assertRaises(RateLimited) as caught:
                await self.run_live(script)
            self.assertEqual((caught.exception.retry_after, caught.exception.stage), (60, 'gemini_live'))
        handshake = lambda status: aiohttp.WSServerHandshakeError(SimpleNamespace(real_url=f'{live.ENDPOINT}'), (), status=status, message=KEY)
        with self.assertRaises(RateLimited):
            await self.run_live([], session=FakeSession(error=handshake(429)))
        for error, code in ((handshake(403), 403), (aiohttp.ClientConnectionError(KEY), 'ClientConnectionError')):
            with self.subTest(code=code), self.assertRaises(RemoteError) as caught:
                await self.run_live([], session=FakeSession(error=error))
            self.assertNotIsInstance(caught.exception, (RateLimited, AuthenticationRequired))
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)

    async def test_setup_close_and_limits(self):
        ws = FakeWS()
        ws.script = [(0, close(1007, 'Invalid model'))]
        with self.assertRaises(RemoteError) as caught:
            await self.run_live([], session=FakeSession(ws))
        self.assertEqual(caught.exception.code, 1007)
        for speed in (0.5, 7, '4'):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                GeminiLiveFileTranscriber(KEY, speed=speed)
        with self.assertRaises(ValueError):
            await GeminiLiveFileTranscriber(KEY, speed=2).transcribe('x.mp3', 0, 1500)


if __name__ == '__main__':
    unittest.main()
