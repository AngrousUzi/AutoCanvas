import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from autocanvas.gemini_transcribe import GeminiTranscriber, segments
from autocanvas.flows import Replay, CatalogSync, save_platform_transcript, transcribe_source_remote
from autocanvas.outputs import Transcript
from autocanvas.types import AuthenticationRequired, MediaError, MediaSource, RateLimited, RemoteError, TranscriptSegment

KEY = 'test-secret-key'


def body(words=(), text=''):
    parts = [{'text': text}] if text else []
    if words:
        parts.append({'audioTranscription': {'words': [{'word': w, 'startOffset': f'{s}s', 'endOffset': f'{e}s'} for w, s, e in words]}})
    return {'candidates': [{'content': {'parts': parts}, 'finishReason': 'STOP'}]}


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self.payload, self.headers = status, payload, headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, generate=None, poll=('ACTIVE',), start=None):
        self.calls, self.start = [], start
        self.generate = generate or Response(payload=body([('你好', 0.1, 0.4), ('。', 0.4, 0.5)]))
        self.poll = list(poll)

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url, dict(headers or {}), kwargs))
        if url.endswith('/upload/v1beta/files'):
            if self.start:
                return self.start
            return Response(headers={'x-goog-upload-url': 'https://generativelanguage.googleapis.com/upload/session?upload_id=1'})
        if 'upload_id=' in url:
            return Response(payload={'file': {'name': 'files/abc', 'uri': 'https://generativelanguage.googleapis.com/v1beta/files/abc', 'state': 'PROCESSING'}})
        if method == 'GET':
            return Response(payload={'name': 'files/abc', 'uri': 'https://generativelanguage.googleapis.com/v1beta/files/abc', 'state': self.poll.pop(0)})
        if url.endswith(':generateContent'):
            if isinstance(self.generate, Exception):
                raise self.generate
            return self.generate
        if method == 'DELETE':
            return Response(payload={})
        raise AssertionError(url)


class Segments(unittest.TestCase):
    def test_split_on_punctuation_gap_and_span(self):
        words = [('今天', 0, 0.3), ('讲', 0.3, 0.5), ('。', 0.5, 0.6), ('Hello', 0.7, 1.0), ('world', 1.1, 1.4),
                 ('然后', 3.0, 3.2)] + [(f'w{i}', 5+i, 5.5+i) for i in range(20)]
        rows = [(round(r.start, 3), round(r.end, 3), r.text) for r in segments(body(words, 'ignored'), 100, 30)]
        self.assertEqual(rows[:3], [(100, 100.6, '今天讲。'), (100.7, 101.4, 'Hello world'), (103, 103.2, '然后')])
        self.assertEqual(rows[3][:2], (105, 120.5))
        self.assertEqual(rows[4], (121, 124.5, 'w16 w17 w18 w19'))
        self.assertEqual(len(rows), 5)

    def test_cjk_ascii_joining_and_clamp(self):
        rows = segments(body([('我们', 0, .2), ('用', .2, .3), ('GPU', .3, .5), ('训练', .5, .7), ('v2', .7, 40)]), 10, 5)
        self.assertEqual(rows, [TranscriptSegment(10, 15, '我们用GPU训练v2')])

    def test_text_fallback_and_empty(self):
        self.assertEqual(segments(body(text='  全文  '), 5, 20), [TranscriptSegment(5, 25, '全文')])
        self.assertEqual(segments(body(), 5, 20), [])
        self.assertEqual(segments({}, 5, 20), [])
        self.assertEqual(segments({'candidates': ['bad']}, 5, 20), [])


class Transcriber(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'a.mp3'
        self.path.write_bytes(b'flac-bytes')

    def tearDown(self):
        self.tmp.cleanup()

    def test_proxy_only_when_set(self):
        proxy = 'http://127.0.0.1:3128'
        self.assertEqual(GeminiTranscriber(KEY, proxy=proxy).session.proxies, {'http': proxy, 'https': proxy})
        for empty in (None, ''):
            self.assertEqual(GeminiTranscriber(KEY, proxy=empty).session.proxies, {})
        self.assertEqual(GeminiTranscriber(KEY).session.proxies, {})

    def test_upload_poll_generate_delete(self):
        session = Session()
        rows = GeminiTranscriber(KEY, session=session, poll_interval=0).transcribe(self.path, 60, 10)
        self.assertEqual([(round(r.start, 3), round(r.end, 3), r.text) for r in rows], [(60.1, 60.5, '你好。')])
        self.assertEqual([c[0] for c in session.calls], ['POST', 'POST', 'GET', 'POST', 'DELETE'])
        start, upload, poll, generate, delete = session.calls
        self.assertEqual(start[2]['X-Goog-Upload-Command'], 'start')
        self.assertEqual(start[2]['X-Goog-Upload-Header-Content-Length'], str(len(b'flac-bytes')))
        self.assertEqual(start[3]['json'], {'file': {'display_name': 'autocanvas'}})
        self.assertEqual(upload[2]['X-Goog-Upload-Command'], 'upload, finalize')
        self.assertEqual(upload[3]['data'], b'flac-bytes')
        self.assertTrue(poll[1].endswith('/v1beta/files/abc'))
        self.assertTrue(generate[1].endswith('/v1beta/models/gemini-3.5-transcribe:generateContent'))
        config = generate[3]['json']['generationConfig']['audioTranscriptionConfig']
        self.assertEqual(config, {'languageCodes': ['cmn-Hans-CN'], 'wordTimestamp': True, 'mode': 'VERBATIM'})
        self.assertEqual(generate[3]['json']['contents'][0]['parts'][0]['fileData']['mimeType'], 'audio/mp3')
        self.assertTrue(delete[1].endswith('/v1beta/files/abc'))
        for method, url, headers, _ in session.calls:
            self.assertNotIn(KEY, url)
        for call in (start, poll, generate, delete):
            self.assertEqual(call[2]['x-goog-api-key'], KEY)

    def test_error_mapping_never_auth_and_hides_key(self):
        for failure in (Response(429, {}), Response(403, {}), Response(401, {}), Response(500, {}), requests.ConnectionError(f'boom {KEY}'),
                        Response(payload=ValueError('bad json')), Response(payload={'candidates': []}),
                        Response(payload={'promptFeedback': {'blockReason': 'SAFETY'}}),
                        Response(payload={'candidates': [{'finishReason': 'SAFETY'}]})):
            with self.subTest(failure=failure):
                session = Session(generate=failure)
                with self.assertRaises(RemoteError) as caught:
                    GeminiTranscriber(KEY, session=session, poll_interval=0).transcribe(self.path, 0, 10)
                self.assertNotIsInstance(caught.exception, AuthenticationRequired)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn(KEY, repr(caught.exception))
                self.assertEqual(caught.exception.stage, 'gemini_generate')
                self.assertEqual(session.calls[-1][0], 'DELETE')
        self.assertNotIn(KEY, repr(GeminiTranscriber(KEY, session=Session())))

    def test_rate_limit_retry_after_and_truncation(self):
        quota = {'error': {'code': 429, 'message': 'You exceeded your current quota. Please retry in 5.6s.', 'status': 'RESOURCE_EXHAUSTED'}}
        for session, stage, wait in ((Session(generate=Response(429, quota)), 'gemini_generate', 7.6),
                                     (Session(start=Response(429, quota)), 'gemini_upload', 7.6),
                                     (Session(generate=Response(429, {'error': {'message': 'quota'}})), 'gemini_generate', None),
                                     (Session(generate=Response(429, ValueError('html'))), 'gemini_generate', None)):
            with self.subTest(stage=stage, wait=wait), self.assertRaises(RateLimited) as caught:
                GeminiTranscriber(KEY, session=session, poll_interval=0).transcribe(self.path, 0, 10)
            self.assertEqual((caught.exception.stage, caught.exception.code), (stage, 429))
            self.assertEqual(caught.exception.retry_after if wait is None else round(caught.exception.retry_after, 3), wait)
        self.assertEqual((GeminiTranscriber.spacing, GeminiTranscriber.name), (65, 'gemini'))
        session = Session(generate=Response(payload={'candidates': [{'content': {'parts': [{'text': '半句'}]}, 'finishReason': 'MAX_TOKENS'}]}))
        with self.assertRaises(RemoteError) as caught:
            GeminiTranscriber(KEY, session=session, poll_interval=0).transcribe(self.path, 0, 10)
        self.assertNotIsInstance(caught.exception, RateLimited)
        self.assertEqual(caught.exception.code, 'MAX_TOKENS')

    def test_file_failed_and_timeout(self):
        session = Session(poll=('FAILED',))
        with self.assertRaises(RemoteError) as caught:
            GeminiTranscriber(KEY, session=session, poll_interval=0).transcribe(self.path, 0, 10)
        self.assertEqual(caught.exception.code, 'FAILED')
        self.assertEqual(session.calls[-1][0], 'DELETE')
        session = Session(poll=('PROCESSING',)*50)
        with self.assertRaises(RemoteError) as caught:
            GeminiTranscriber(KEY, session=session, poll_interval=0, upload_wait=0).transcribe(self.path, 0, 10)
        self.assertEqual(caught.exception.code, 'timeout')


class Flow(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_offsets_resume_and_short_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder, cache = root/'out', root/'cache'
            folder.mkdir()
            (folder/'segments.jsonl').write_text(json.dumps({'start': 0, 'end': 50, 'text': '旧'})+'\n', encoding='utf-8')
            total = 50+100+100+30
            extracted, calls = [], []
            async def audio_file(source, destination, *, offset=0, duration):
                extracted.append((offset, duration))
                destination.write_bytes(b'x')
                return max(0.0, min(duration, total-offset))
            class Fake:
                spacing, name = 0, 'fake'
                def transcribe(self, path, offset, duration):
                    calls.append((offset, duration))
                    self.existed = path.exists()
                    return [TranscriptSegment(offset+1, offset+duration-1, f'段{offset:g}')]
            fake = Fake()
            with patch('autocanvas.flows.media.audio_file', audio_file):
                result = await transcribe_source_remote(MediaSource('x'), [fake], folder, cache, chunk_seconds=100)
            self.assertEqual(extracted, [(50, 100), (150, 100), (250, 100)])
            self.assertEqual(calls, [(50, 100), (150, 100), (250, 30)])
            self.assertTrue(fake.existed)
            rows = json.loads(result.read_text(encoding='utf-8'))
            self.assertEqual([(r['start'], r['text']) for r in rows], [(0, '旧'), (51, '段50'), (151, '段150'), (251, '段250')])
            self.assertFalse(cache.exists())
            self.assertIn('[00:02:31] 段150', (folder/'transcript.txt').read_text(encoding='utf-8'))

    async def test_duration_limit_and_empty_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted = []
            async def audio_file(source, destination, *, offset=0, duration):
                extracted.append((offset, duration))
                return duration if offset < 200 else 0.2
            fake = SimpleNamespace(spacing=0, name='fake', transcribe=lambda path, offset, duration: [TranscriptSegment(offset, offset+duration, 't')])
            with patch('autocanvas.flows.media.audio_file', audio_file):
                await transcribe_source_remote(MediaSource('x'), [fake], root/'a', root/'c1', chunk_seconds=100, duration=150)
                self.assertEqual(extracted, [(0, 100), (100, 50)])
                extracted.clear()
                await transcribe_source_remote(MediaSource('x'), [fake], root/'b', root/'c2', chunk_seconds=100)
                self.assertEqual(extracted, [(0, 100), (100, 100), (200, 100)])

    async def test_replay_without_published_media_is_not_ready(self):
        from autocanvas.types import MediaNotReady
        async def resolve(lecture): return []
        with tempfile.TemporaryDirectory() as tmp:
            replay = Replay(resolve, None, Path(tmp)/'outputs', Path(tmp)/'cache')
            with self.assertRaises(MediaNotReady):
                await replay.run({'id': '2', 'course_id': '1', 'kind': 'vod'}, 'vod_slides', {})

    async def test_replay_dispatch_and_platform_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lecture = {'id': '2', 'course_id': '1', 'kind': 'vod'}
            async def resolve(lecture): return [MediaSource('x')]
            async def select(sources, purpose, view): return sources[0]
            async def failing(lecture): raise AuthenticationRequired('expired')
            seen = []
            async def remote(source, transcribers, folder, cache, **kwargs):
                seen.append((transcribers, folder, cache, kwargs))
                return folder/'transcript.json'
            replay = Replay(resolve, None, root/'outputs', root/'cache', platform_transcript=failing, transcribers=('T',), remote_chunk_seconds=900)
            with patch('autocanvas.flows.media.select', select), patch('autocanvas.flows.transcribe_source_remote', remote), self.assertLogs('autocanvas.flows', 'WARNING') as logs:
                result = await replay.run(lecture, 'vod_asr', {'duration': 60})
            self.assertEqual(result, root/'outputs'/'1'/'2'/'vod_asr'/'transcript.json')
            self.assertEqual(seen, [(['T'], root/'outputs'/'1'/'2'/'vod_asr', root/'cache'/'asr'/'1'/'2', {'chunk_seconds': 900, 'duration': 60, 'refresh': seen[0][3]['refresh']})])
            self.assertEqual(await seen[0][3]['refresh'](), MediaSource('x'))
            self.assertIn('AuthenticationRequired', logs.output[0])
            self.assertNotIn('expired', logs.output[0])
            async def platform(lecture):
                return {'beforeAssemblyList': [{'bg': 1500, 'ed': 3000, 'res': '平台'}]}
            local = []
            async def transcribe_source(source, transcribe, folder, **kwargs):
                local.append(kwargs)
                return folder/'transcript.json'
            replay = Replay(resolve, 'recognize', root/'outputs', root/'cache', chunk_seconds=3, platform_transcript=platform)
            with patch('autocanvas.flows.media.select', select), patch('autocanvas.flows.transcribe_source', transcribe_source):
                await replay.run(lecture, 'vod_asr', {})
            self.assertEqual(local, [{'chunk_seconds': 3, 'duration': None}])
            self.assertEqual(json.loads((root/'outputs'/'1'/'2'/'platform'/'transcript.json').read_text(encoding='utf-8')), [{'start': 1.5, 'end': 3.0, 'text': '平台'}])


class ShortChunk(unittest.IsolatedAsyncioTestCase):
    """Lecture is 250 s, chunks are 100 s; a partial ffmpeg output must not end the lecture."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.refreshed, self.short = [], [], {}
        async def audio_file(source, destination, *, offset=0, duration):
            self.calls.append((source.location, offset))
            destination.write_bytes(b'x')
            n = self.short.get(offset)
            if n:
                self.short[offset] -= 1
                return 30.0
            return max(0.0, min(duration, 250-offset))
        async def probe(source):
            if self.probe_error:
                raise MediaError('nope')
            return {'format': {'duration': '250.0'}}
        async def refresh():
            self.refreshed.append(1)
            return MediaSource(f'fresh{len(self.refreshed)}')
        self.refresh, self.probe_error = refresh, False
        async def sleep(seconds):
            if seconds:
                self.waits.append(seconds)
        self.waits = []
        for target in (patch('autocanvas.flows.media.audio_file', audio_file), patch('autocanvas.flows.media.probe', probe),
                       patch('autocanvas.flows.asyncio.sleep', sleep)):
            target.start()
            self.addCleanup(target.stop)
        self.fake = SimpleNamespace(spacing=0, name='fake', transcribe=lambda path, offset, duration: [TranscriptSegment(offset, offset+duration, f'{offset:g}')])

    async def test_short_chunk_is_retried_with_fresh_source(self):
        self.short = {100: 2}
        result = await transcribe_source_remote(MediaSource('x'), [self.fake], self.root/'o', self.root/'c', chunk_seconds=100, refresh=self.refresh)
        self.assertEqual([c[1] for c in self.calls], [0, 100, 100, 100, 200])
        self.assertEqual(len(self.refreshed), 5)
        self.assertEqual(self.calls[2][0], 'fresh3')
        self.assertEqual(self.waits, [5, 15])
        rows = json.loads(result.read_text(encoding='utf-8'))
        self.assertEqual([r['start'] for r in rows], [0, 100, 200])
        self.assertEqual(rows[-1]['end'], 250)

    async def test_persistently_short_chunk_raises_and_keeps_earlier_chunks(self):
        self.short = {100: 99}
        with self.assertRaises(MediaError) as caught:
            await transcribe_source_remote(MediaSource('x'), [self.fake], self.root/'o', self.root/'c', chunk_seconds=100, refresh=self.refresh)
        self.assertIn('stopped early at 130s of 250s', str(caught.exception))
        self.assertEqual(self.waits, [5, 15, 30])
        self.assertEqual(len([c for c in self.calls if c[1] == 100]), 4)
        rows = json.loads((self.root/'o'/'transcript.json').read_text(encoding='utf-8'))
        self.assertEqual([r['start'] for r in rows], [0])
        self.assertEqual(Transcript(self.root/'o').end, 100)

    async def test_true_end_is_not_short(self):
        await transcribe_source_remote(MediaSource('x'), [self.fake], self.root/'o', self.root/'c', chunk_seconds=100)
        self.assertEqual([c[1] for c in self.calls], [0, 100, 200])
        self.assertEqual(self.waits, [])

    async def test_probe_failure_keeps_old_behavior(self):
        self.probe_error, self.short = True, {100: 1}
        await transcribe_source_remote(MediaSource('x'), [self.fake], self.root/'o', self.root/'c', chunk_seconds=100)
        self.assertEqual([c[1] for c in self.calls], [0, 100])
        self.assertEqual(self.waits, [])

    async def test_slides_stopped_early_raises(self):
        from autocanvas.flows import slides_source
        async def sample(source, cache, *, every, duration=None):
            for i in range(10):
                (cache/f'{i:08d}.jpg').write_bytes(b'x')
        with patch('autocanvas.flows.media.sample_frames', sample):
            with self.assertRaises(MediaError) as caught:
                await slides_source(MediaSource('x'), self.root/'s', self.root/'sc', sample_every=5)
            self.assertIn('Frame sampling stopped early', str(caught.exception))
            self.assertFalse((self.root/'sc').exists())


class FakeTranscriber:
    def __init__(self, name, spacing, failures=()):
        self.name, self.spacing, self.failures, self.calls = name, spacing, list(failures), []

    def transcribe(self, path, offset, duration):
        self.calls.append(offset)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        return [TranscriptSegment(offset+1, offset+duration-1, f'{self.name}{offset:g}')]


class AsyncTranscriber(FakeTranscriber):
    async def transcribe(self, path, offset, duration):
        return FakeTranscriber.transcribe(self, path, offset, duration)


class RemoteFlow(unittest.IsolatedAsyncioTestCase):
    """Virtual clock: asyncio.sleep advances flows' monotonic time instead of waiting."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock, self.waits, self.extracted, self.total = [0.0], [], [], 250
        real_sleep = asyncio.sleep
        async def sleep(seconds):
            self.waits.append(round(seconds, 3))
            self.clock[0] += seconds
            await real_sleep(0)
        async def audio_file(source, destination, *, offset=0, duration):
            self.extracted.append(offset)
            destination.write_bytes(b'x')
            return max(0.0, min(duration, self.total-offset))
        clock = SimpleNamespace(monotonic=lambda: self.clock[0], time=lambda: 1000.0)
        for target in (patch('autocanvas.flows.asyncio.sleep', sleep), patch('autocanvas.flows.time', clock), patch('autocanvas.flows.media.audio_file', audio_file)):
            target.start()
            self.addCleanup(target.stop)

    def tearDown(self):
        self.tmp.cleanup()

    async def run_job(self, transcribers, name='out'):
        await transcribe_source_remote(MediaSource('x'), transcribers, self.root/name, self.root/'cache', chunk_seconds=100)
        events = self.root/name/'events.jsonl'
        return [json.loads(line) for line in events.read_text(encoding='utf-8').splitlines()] if events.exists() else []

    async def test_pacing_between_chunks(self):
        gemini = FakeTranscriber('gemini', 65)
        events = await self.run_job([gemini])
        self.assertEqual(self.waits, [0, 65, 65])
        self.assertEqual(gemini.calls, [0, 100, 200])
        self.assertEqual([(e['type'], e['start'], e['end'], e['backend'], e['at']) for e in events],
                         [('chunk', 0, 100, 'gemini', 1000.0), ('chunk', 100, 200, 'gemini', 1000.0), ('chunk', 200, 250, 'gemini', 1000.0)])

    async def test_short_rate_limit_retries_same_transcriber(self):
        self.total = 100
        gemini, groq = FakeTranscriber('gemini', 65, [RateLimited(retry_after=7.6), RateLimited()]), FakeTranscriber('groq', 3)
        events = await self.run_job([gemini, groq])
        # Retry wait and pacing overlap: 7.6 s retry, then the rest of the 65 s spacing; unknown wait backs off 2 s.
        self.assertEqual(self.waits, [0, 7.6, 57.4, 2, 63])
        self.assertEqual((gemini.calls, groq.calls), ([0, 0, 0], []))
        self.assertEqual([e['backend'] for e in events], ['gemini'])

    async def test_long_rate_limit_falls_back_and_block_persists(self):
        gemini, groq = FakeTranscriber('gemini', 65, [RateLimited(retry_after=3000)]), FakeTranscriber('groq', 3)
        events = await self.run_job([gemini, groq])
        self.assertEqual((gemini.calls, groq.calls), ([0], [0, 100, 200]))
        self.assertEqual([e['backend'] for e in events], ['groq', 'groq', 'groq'])
        self.assertEqual(gemini.blocked_until, 3000)
        events = await self.run_job([gemini, groq], 'second')
        self.assertEqual(gemini.calls, [0])
        self.assertEqual([e['backend'] for e in events], ['groq']*3)
        self.clock[0] = 3001
        events = await self.run_job([gemini, groq], 'third')
        self.assertEqual(gemini.calls, [0, 0, 100, 200])
        self.assertEqual([e['backend'] for e in events], ['gemini']*3)
        rows = json.loads((self.root/'third'/'transcript.json').read_text(encoding='utf-8'))
        self.assertEqual([r['text'] for r in rows], ['gemini0', 'gemini100', 'gemini200'])

    async def test_all_blocked_raises_and_resume(self):
        gemini = FakeTranscriber('gemini', 0, [None, RateLimited(retry_after=500)])
        groq = FakeTranscriber('groq', 0, [RateLimited()]*5)
        with self.assertRaises(RateLimited) as caught:
            await self.run_job([gemini, groq])
        self.assertEqual([w for w in self.waits if w], [1, 2, 4, 8])
        # Gemini was blocked at t=0 for 500 s; Groq then spent 15 s backing off, so 485 s remain.
        self.assertEqual((gemini.blocked_until, round(caught.exception.retry_after, 3)), (500, 485))
        self.assertEqual(groq.blocked_until-self.clock[0], 3600)
        self.assertFalse((self.root/'cache').exists())
        self.assertEqual([r['text'] for r in json.loads((self.root/'out'/'transcript.json').read_text(encoding='utf-8'))], ['gemini0'])
        with self.assertRaises(RateLimited):
            await self.run_job([gemini, groq])
        # Within a job chunks advance by real length; a new job resumes from the last saved segment end.
        self.assertEqual(gemini.calls, [0, 100])
        self.clock[0] += 600
        self.extracted.clear()
        events = await self.run_job([gemini, groq])
        self.assertEqual(self.extracted, [99, 199])
        self.assertEqual(gemini.calls, [0, 100, 99, 199])
        self.assertEqual([r['text'] for r in json.loads((self.root/'out'/'transcript.json').read_text(encoding='utf-8'))], ['gemini0', 'gemini99', 'gemini199'])
        self.assertEqual([(e['start'], e['backend']) for e in events], [(0, 'gemini'), (99, 'gemini'), (199, 'gemini')])

    async def test_async_transcriber_awaited_and_sync_fallback(self):
        live, groq, threaded = AsyncTranscriber('gemini-live', 0, [None, RateLimited(retry_after=3000)]), FakeTranscriber('groq', 3), []
        async def blocking(function, *args):
            threaded.append(function.__self__.name)
            return function(*args)
        with patch('autocanvas.flows.blocking', blocking):
            events = await self.run_job([live, groq])
        self.assertEqual((live.calls, groq.calls, threaded), ([0, 100], [100, 200], ['groq', 'groq']))
        self.assertEqual([e['backend'] for e in events], ['gemini-live', 'groq', 'groq'])

    async def test_other_remote_error_propagates(self):
        gemini, groq = FakeTranscriber('gemini', 0, [RemoteError('boom', stage='gemini_generate', code=500)]), FakeTranscriber('groq', 0)
        with self.assertRaises(RemoteError) as caught:
            await self.run_job([gemini, groq])
        self.assertNotIsInstance(caught.exception, RateLimited)
        self.assertEqual(groq.calls, [])
        self.assertEqual(gemini.next_at, 0)


class Platform(unittest.TestCase):
    def test_conversion_fallback_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)/'platform'
            data = {'beforeAssemblyList': [{'bg': 61000, 'ed': 62500, 'res': '第二句'}, {'bg': 1000, 'ed': 2000, 'res': '第一句'}, {'bg': 3000, 'ed': 4000, 'res': ' '}],
                    'afterAssemblyList': [{'bg': 0, 'ed': 9000, 'res': '段落'}]}
            self.assertEqual(save_platform_transcript(data, folder), folder/'transcript.json')
            self.assertEqual(json.loads((folder/'transcript.json').read_text(encoding='utf-8')), [{'start': 1.0, 'end': 2.0, 'text': '第一句'}, {'start': 61.0, 'end': 62.5, 'text': '第二句'}])
            self.assertEqual(json.loads((folder/'raw.json').read_text(encoding='utf-8')), data)
            self.assertEqual((folder/'transcript.txt').read_text(encoding='utf-8'), '[00:00:01] 第一句\n[00:01:01] 第二句\n')
            save_platform_transcript({'beforeAssemblyList': [], 'afterAssemblyList': [{'bg': 0, 'ed': 9000, 'res': '段落'}]}, folder)
            self.assertEqual(json.loads((folder/'transcript.json').read_text(encoding='utf-8')), [{'start': 0.0, 'end': 9.0, 'text': '段落'}])
            empty = Path(tmp)/'empty'
            for data in (None, {}, {'beforeAssemblyList': None, 'afterAssemblyList': []}, {'beforeAssemblyList': [{'bg': 0, 'ed': 1, 'res': ''}]}):
                self.assertIsNone(save_platform_transcript(data, empty))
            self.assertFalse(empty.exists())

    def test_video_and_catalog_retry(self):
        from autocanvas.video import Video
        video = Video(SimpleNamespace(session=None, token='t'))
        calls = []
        video.get = lambda path, **params: calls.append((path, params)) or None
        self.assertIsNone(video.transcript('42'))
        self.assertEqual(calls, [('/v1/course/ai/translate/42', {'useOriginal': 'true'})])
        attempts = []
        class Client:
            def transcript(self, lecture_id):
                attempts.append(lecture_id)
                if len(attempts) == 1:
                    raise AuthenticationRequired()
                return {'ok': 1}
            def close(self): pass
        catalog = CatalogSync(None, lambda course: Client(), None)
        self.assertEqual(catalog.transcript({'id': '2', 'course_id': '1'}), {'ok': 1})
        self.assertEqual(attempts, ['2', '2'])


class Backfill(unittest.IsolatedAsyncioTestCase):
    async def test_recent_missing_vod_lectures_only(self):
        from datetime import datetime, timedelta, timezone
        from autocanvas.config import Settings
        from autocanvas.service import Service
        from autocanvas.storage import Store
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/'state.sqlite3')
            now = datetime.now(timezone.utc)
            rows = {'new': (now, 'vod'), 'old': (now-timedelta(days=30), 'vod'), 'live': (now, 'live'), 'done': (now, 'vod'), 'boom': (now, 'vod')}
            for key, (begin, kind) in rows.items():
                store.put('lectures', f'1:{kind}:{key}', {'id': key, 'course_id': '1', 'kind': kind, 'begin': begin.isoformat()})
            done = root/'outputs'/'1'/'done'/'platform'
            done.mkdir(parents=True)
            (done/'transcript.json').write_text('[]', encoding='utf-8')
            seen = []
            def transcript(lecture):
                seen.append(lecture['id'])
                if lecture['id'] == 'boom':
                    raise AuthenticationRequired()
                return {'beforeAssemblyList': [{'bg': 1000, 'ed': 2000, 'res': '新课'}]}
            service = Service(Settings(root=root).validate(), store, SimpleNamespace(transcript=transcript), None, None, None)
            await service._platform_transcripts({'1'})
            self.assertEqual(sorted(seen), ['boom', 'new'])
            self.assertEqual(json.loads((root/'outputs'/'1'/'new'/'platform'/'transcript.json').read_text(encoding='utf-8')),
                             [{'start': 1.0, 'end': 2.0, 'text': '新课'}])
