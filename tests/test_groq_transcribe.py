import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from autocanvas.groq_transcribe import API, PROMPT, GroqTranscriber, clean
from autocanvas.types import AuthenticationRequired, RateLimited, RemoteError, TranscriptSegment

KEY = 'groq-secret-key'


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self.payload, self.headers = status, payload, headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        name, handle, mime = files['file']
        self.calls.append({'url': url, 'headers': dict(headers), 'data': dict(data), 'file': (name, handle.read(), mime), 'timeout': timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def seg(start, end, text, no_speech=0.0, logprob=-0.2):
    return {'start': start, 'end': end, 'text': text, 'no_speech_prob': no_speech, 'avg_logprob': logprob}


class Proxy(unittest.TestCase):
    def test_proxy_only_when_set(self):
        proxy = 'http://127.0.0.1:3128'
        self.assertEqual(GroqTranscriber(KEY, proxy=proxy).session.proxies, {'http': proxy, 'https': proxy})
        for empty in (None, ''):
            self.assertEqual(GroqTranscriber(KEY, proxy=empty).session.proxies, {})
        self.assertEqual(GroqTranscriber(KEY).session.proxies, {})


class Clean(unittest.TestCase):
    def test_repeats_silence_and_punctuation(self):
        rows = [seg(0, 1, ' 好的 '), seg(1, 2, '好的'), seg(2, 3, '好的'), seg(3, 4, '好的'), seg(4, 5, '  '),
                seg(5, 6, '静音', 0.95, -1.5), seg(6, 7, '保留', 0.95, -0.5), seg(7, 8, '对，，，对。。'), seg(8, 9, 'wait...!!')]
        self.assertEqual([r['text'] for r in clean(rows)], ['好的', '好的', '保留', '对，对。', 'wait.!'])
        self.assertEqual(clean([None, {'text': None}]), [])


class Transcriber(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'000001800000.mp3'
        self.path.write_bytes(b'mp3-bytes')

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_shape_offsets_and_clamp(self):
        payload = {'segments': [seg(-0.5, 2, '开头'), seg(2, 4, '中间'), seg(4, 4.5, '中间'), seg(4.5, 5, '中间'), seg(58, 75, '结尾'),
                                seg(5, 6, ''), {'start': 'x', 'end': 1, 'text': '坏'}]}
        session = Session(Response(payload=payload))
        rows = GroqTranscriber(KEY, session=session).transcribe(self.path, 1800, 60)
        self.assertEqual(rows, [TranscriptSegment(1800, 1802, '开头'), TranscriptSegment(1802, 1804, '中间'),
                                TranscriptSegment(1804, 1804.5, '中间'), TranscriptSegment(1858, 1860, '结尾')])
        call, = session.calls
        self.assertEqual(call['url'], API)
        self.assertNotIn(KEY, call['url'])
        self.assertEqual(call['headers'], {'Authorization': f'Bearer {KEY}'})
        self.assertEqual(call['data'], {'model': 'whisper-large-v3-turbo', 'temperature': '0', 'response_format': 'verbose_json',
                                        'timestamp_granularities[]': 'segment', 'language': 'zh', 'prompt': PROMPT})
        self.assertEqual(call['file'], ('000001800000.mp3', b'mp3-bytes', 'audio/mpeg'))
        self.assertEqual(call['timeout'], 300)
        self.assertEqual(PROMPT, '以下是普通话的课堂内容，请使用简体中文和标点符号。')
        self.assertEqual((GroqTranscriber.spacing, GroqTranscriber.name), (3, 'groq'))
        session = Session(Response(payload={'segments': []}))
        GroqTranscriber(KEY, model='whisper-large-v3', language='auto', prompt='', session=session).transcribe(self.path, 0, 10)
        self.assertEqual(session.calls[0]['data'], {'model': 'whisper-large-v3', 'temperature': '0', 'response_format': 'verbose_json',
                                                    'timestamp_granularities[]': 'segment'})

    def test_rate_limit_uses_retry_after_header(self):
        for headers, wait in (({'Retry-After': '17'}, 17.0), ({'Retry-After': 'soon'}, None), ({}, None)):
            with self.subTest(headers=headers), self.assertRaises(RateLimited) as caught:
                GroqTranscriber(KEY, session=Session(Response(429, {}, headers))).transcribe(self.path, 0, 10)
            self.assertEqual((caught.exception.retry_after, caught.exception.stage, caught.exception.code), (wait, 'groq', 429))

    def test_errors_never_auth_and_hide_key(self):
        for failure, code in ((Response(401, {}), 401), (Response(403, {}), 403), (Response(413, {}), 413), (Response(500, {}), 500),
                              (requests.ConnectionError(f'boom {KEY}'), 'ConnectionError'), (requests.Timeout(KEY), 'Timeout'),
                              (Response(payload=ValueError(KEY)), 'invalid_json'), (Response(payload=[]), 'invalid_json'),
                              (Response(payload={'text': 'no segments'}), 'invalid_json')):
            with self.subTest(failure=failure), self.assertRaises(RemoteError) as caught:
                GroqTranscriber(KEY, session=Session(failure)).transcribe(self.path, 0, 10)
            error = caught.exception
            self.assertNotIsInstance(error, (AuthenticationRequired, RateLimited))
            self.assertEqual((error.stage, error.code), ('groq', code))
            self.assertNotIn(KEY, str(error))
            self.assertNotIn(KEY, repr(error))
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__ or error.__context__ is None)
        self.assertNotIn(KEY, repr(GroqTranscriber(KEY)))
        for bad in ({'api_key': ''}, {'api_key': KEY, 'model': 'bad/model'}):
            with self.assertRaises(ValueError):
                GroqTranscriber(**bad)


class Bootstrap(unittest.TestCase):
    NAMES = ('AUTOCANVAS_REPLAY_ASR_BACKEND', 'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GROQ_API_KEY', 'AUTOCANVAS_REPLAY_CHUNK_SECONDS',
             'AUTOCANVAS_GROQ_MODEL', 'AUTOCANVAS_GROQ_LANGUAGE', 'AUTOCANVAS_GROQ_PROMPT', 'AUTOCANVAS_GEMINI_REPLAY_MODEL', 'AUTOCANVAS_GEMINI_REPLAY_LANGUAGE',
             'AUTOCANVAS_GEMINI_REPLAY_LIVE_MODEL', 'AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED', 'AUTOCANVAS_ASR_PROXY')

    def replay(self, **values):
        from autocanvas.bootstrap import replay_asr
        with patch.dict(os.environ, {}):
            for name in self.NAMES:
                os.environ.pop(name, None)
            os.environ.update(values)
            return replay_asr()

    def test_asr_proxy_reaches_all_replay_clients(self):
        proxy = 'http://127.0.0.1:3128'
        base = dict(AUTOCANVAS_REPLAY_ASR_BACKEND='gemini-live, gemini, groq', GEMINI_API_KEY='g', GROQ_API_KEY='q')
        live, gemini, groq = self.replay(AUTOCANVAS_ASR_PROXY=proxy, **base)['transcribers']
        self.assertEqual((live.proxy, gemini.session.proxies, groq.session.proxies), (proxy, {'http': proxy, 'https': proxy}, {'http': proxy, 'https': proxy}))
        for values in ({}, {'AUTOCANVAS_ASR_PROXY': ''}):
            live, gemini, groq = self.replay(**base, **values)['transcribers']
            self.assertEqual((live.proxy, gemini.session.proxies, groq.session.proxies), (None, {}, {}))

    def test_backend_list(self):
        self.assertEqual(self.replay(), {})
        self.assertEqual(self.replay(AUTOCANVAS_REPLAY_ASR_BACKEND='local'), {})
        result = self.replay(AUTOCANVAS_REPLAY_ASR_BACKEND='gemini, groq', GEMINI_API_KEY='g', GROQ_API_KEY='q', AUTOCANVAS_GROQ_MODEL='whisper-large-v3',
                             AUTOCANVAS_GROQ_PROMPT='提示', AUTOCANVAS_REPLAY_CHUNK_SECONDS='1680')
        self.assertEqual([t.name for t in result['transcribers']], ['gemini', 'groq'])
        self.assertEqual((result['transcribers'][1].model, result['transcribers'][1].language, result['transcribers'][1].prompt), ('whisper-large-v3', 'zh', '提示'))
        self.assertEqual(result['remote_chunk_seconds'], 1680)
        result = self.replay(AUTOCANVAS_REPLAY_ASR_BACKEND='groq', GROQ_API_KEY='q')
        self.assertEqual(([t.name for t in result['transcribers']], result['transcribers'][0].prompt, result['remote_chunk_seconds']), (['groq'], PROMPT, 1500))
        for values, message in (({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'local,groq', 'GROQ_API_KEY': 'q'}, 'AUTOCANVAS_REPLAY_ASR_BACKEND'),
                                ({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'groq,groq', 'GROQ_API_KEY': 'q'}, 'AUTOCANVAS_REPLAY_ASR_BACKEND'),
                                ({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'whisper'}, 'AUTOCANVAS_REPLAY_ASR_BACKEND'),
                                ({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'gemini,groq', 'GEMINI_API_KEY': 'g'}, 'GROQ_API_KEY'),
                                ({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'groq,gemini', 'GROQ_API_KEY': 'q'}, 'GEMINI_API_KEY'),
                                ({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'groq', 'GROQ_API_KEY': 'q', 'AUTOCANVAS_REPLAY_CHUNK_SECONDS': '1800'}, 'AUTOCANVAS_REPLAY_CHUNK_SECONDS'),
                                ({'AUTOCANVAS_REPLAY_ASR_BACKEND': 'groq', 'GROQ_API_KEY': 'q', 'AUTOCANVAS_REPLAY_CHUNK_SECONDS': 'x'}, 'AUTOCANVAS_REPLAY_CHUNK_SECONDS')):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, message):
                self.replay(**values)

    def test_gemini_live_speed_and_chunk(self):
        result = self.replay(AUTOCANVAS_REPLAY_ASR_BACKEND='gemini-live,gemini,groq', GEMINI_API_KEY='g', GROQ_API_KEY='q', AUTOCANVAS_GEMINI_REPLAY_LANGUAGE='en-US')
        live = result['transcribers'][0]
        self.assertEqual([t.name for t in result['transcribers']], ['gemini-live', 'gemini', 'groq'])
        self.assertEqual((live.model, live.language, live.speed, live.spacing, result['remote_chunk_seconds']), ('models/gemini-3.5-transcribe-live', 'en-US', 4, 0, 1500))
        self.assertEqual(result['transcribers'][1].language, 'en-US')
        live = self.replay(AUTOCANVAS_REPLAY_ASR_BACKEND='gemini-live', GOOGLE_API_KEY='g', AUTOCANVAS_GEMINI_REPLAY_LIVE_MODEL='models/x-live',
                           AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED='2', AUTOCANVAS_REPLAY_CHUNK_SECONDS='1080')['transcribers'][0]
        self.assertEqual((live.model, live.speed), ('models/x-live', 2))
        for values in ({}, {'GEMINI_API_KEY': 'g', 'AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED': '7'}, {'GEMINI_API_KEY': 'g', 'AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED': '0.5'},
                       {'GEMINI_API_KEY': 'g', 'AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED': 'fast'},
                       {'GEMINI_API_KEY': 'g', 'AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED': '2', 'AUTOCANVAS_REPLAY_CHUNK_SECONDS': '1500'}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'GEMINI_API_KEY' if not values else 'AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED'):
                self.replay(AUTOCANVAS_REPLAY_ASR_BACKEND='gemini-live', **values)


if __name__ == '__main__':
    unittest.main()
