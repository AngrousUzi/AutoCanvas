"""Groq Whisper request transcription of recorded audio files."""
import re
from pathlib import Path
import requests
from .types import RateLimited, RemoteError, TranscriptSegment

API = 'https://api.groq.com/openai/v1/audio/transcriptions'
PROMPT = '以下是普通话的课堂内容，请使用简体中文和标点符号。'
PUNCT = re.compile(r'([。！？，、,.!?~…])\1+')


def clean(rows):
    # Ported from va2md: drop Whisper repeat loops (third and later) and confident silence; collapse punctuation runs.
    result, previous, run = [], None, 0
    for row in rows:
        text = str(row.get('text') or '').strip() if isinstance(row, dict) else ''
        if not text:
            continue
        run, previous = (run+1 if text == previous else 0), text
        if run >= 2 or ((row.get('no_speech_prob') or 0) > 0.9 and (row.get('avg_logprob') or 0) < -1.0):
            continue
        result.append({**row, 'text': PUNCT.sub(r'\1', text)})
    return result


def segments(body, offset, duration):
    limit = offset+duration
    result = []
    for row in clean(body.get('segments') or []):
        try:
            start, end = float(row['start']), float(row['end'])
        except (KeyError, TypeError, ValueError):
            continue
        start = min(max(offset+start, offset), limit)
        result.append(TranscriptSegment(start, min(max(offset+end, start), limit), row['text']))
    return result


def retry_after(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GroqTranscriber:
    # Free tier: 20 req/min, 2,000 req/day, 7,200 audio-s/hour, 28,800 audio-s/day, 25 MB per file.
    spacing, name = 3, 'groq'

    def __init__(self, api_key, model='whisper-large-v3-turbo', language='zh', prompt=PROMPT, session=None, timeout=300, proxy=None):
        if not api_key:
            raise ValueError('Groq API key required')
        if not re.fullmatch(r'[A-Za-z0-9._-]+', model or ''):
            raise ValueError('Invalid Groq model name')
        self._key, self.model, self.language, self.prompt = api_key, model, language, prompt
        self.session, self.timeout = session or requests.Session(), timeout
        if proxy:
            self.session.proxies = {'http': proxy, 'https': proxy}

    def __repr__(self):
        return f'GroqTranscriber(model={self.model!r}, language={self.language!r})'

    def transcribe(self, path, offset, duration):
        path = Path(path)
        data = {'model': self.model, 'temperature': '0', 'response_format': 'verbose_json', 'timestamp_granularities[]': 'segment'}
        if self.language and self.language != 'auto':
            data['language'] = self.language
        if self.prompt:
            data['prompt'] = self.prompt
        try:
            with path.open('rb') as handle:
                response = self.session.post(API, headers={'Authorization': f'Bearer {self._key}'}, data=data,
                                             files={'file': (path.name, handle, 'audio/mpeg')}, timeout=self.timeout)
        except requests.RequestException as error:
            raise RemoteError('groq transport failed', stage='groq', code=type(error).__name__) from None
        if response.status_code == 429:
            raise RateLimited('groq rate limited', stage='groq', retry_after=retry_after(response.headers.get('Retry-After')))
        if not 200 <= response.status_code < 300:
            raise RemoteError(f'groq HTTP {response.status_code}', stage='groq', code=response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise RemoteError('groq response is not JSON', stage='groq', code='invalid_json') from None
        if not isinstance(body, dict) or not isinstance(body.get('segments'), list):
            raise RemoteError('groq response has no segments', stage='groq', code='invalid_json')
        return segments(body, offset, duration)
