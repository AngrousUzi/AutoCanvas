"""Gemini request/response transcription of recorded audio files; not the Live API."""
import re
import time
from pathlib import Path
import requests
from .types import RateLimited, RemoteError, TranscriptSegment

API = 'https://generativelanguage.googleapis.com'
MIME = 'audio/mp3'
RETRY = re.compile(r'retry in ([\d.]+)s')
STOPS = tuple('。！？!?；;…')
BLOCKED = {'SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'OTHER'}


def seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.endswith('s'):
        try:
            return float(value[:-1])
        except ValueError:
            return None
    return None


def retry_after(response):
    # Gemini 429s carry no Retry-After header; the wait is only in error.message.
    try:
        message = (response.json().get('error') or {}).get('message') or ''
    except (ValueError, AttributeError):
        message = getattr(response, 'text', '') or ''
    found = RETRY.search(str(message))
    return float(found.group(1))+2 if found else None


def join(text, token):
    if text and token and text[-1].isascii() and not text[-1].isspace() and token[0].isascii() and token[0].isalnum():
        return text+' '+token
    return text+token


def segments(body, offset, duration):
    candidates = body.get('candidates') if isinstance(body, dict) else None
    first = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    content = first.get('content') if isinstance(first.get('content'), dict) else {}
    parts = [p for p in content.get('parts') or [] if isinstance(p, dict)] if isinstance(content.get('parts'), list) else []
    text = ''.join(p['text'] for p in parts if isinstance(p.get('text'), str)).strip()
    words = []
    for part in parts:
        info = part.get('audioTranscription')
        found = info.get('words') if isinstance(info, dict) else None
        for word in found if isinstance(found, list) else []:
            token = str(word.get('word') or word.get('text') or '').strip() if isinstance(word, dict) else ''
            if token:
                words.append((token, seconds(word.get('startOffset')), seconds(word.get('endOffset'))))
    limit = offset+duration if duration else None
    clamp = lambda t: min(max(offset+t, offset), limit) if limit is not None else max(offset+t, offset)
    if not words:
        return [TranscriptSegment(offset, offset+(duration or 0), text)] if text else []
    result, current, begin, last = [], '', None, 0.0
    def flush():
        nonlocal current, begin
        if current:
            result.append(TranscriptSegment(clamp(begin), clamp(max(last, begin)), current))
        current, begin = '', None
    for token, start, end in words:
        start = last if start is None else start
        end = start if end is None else max(end, start)
        if current and start-last >= 1.0:
            flush()
        if begin is None:
            begin = start
        current, last = join(current, token), end
        if token.endswith(STOPS) or last-begin >= 15:
            flush()
    flush()
    return result


class GeminiTranscriber:
    # Free tier: 3 req/min charged at completion, so the next request starts 65 s after the last one ended.
    spacing, name = 65, 'gemini'

    def __init__(self, api_key, model='gemini-3.5-transcribe', language='cmn-Hans-CN', session=None, timeout=900, upload_wait=120, poll_interval=2, proxy=None):
        if not api_key:
            raise ValueError('Gemini API key required')
        if not re.fullmatch(r'[A-Za-z0-9._-]+', model or ''):
            raise ValueError('Invalid Gemini model name')
        self._key, self.model, self.language = api_key, model, language
        self.session = session or requests.Session()
        if proxy:
            self.session.proxies = {'http': proxy, 'https': proxy}
        self.timeout, self.upload_wait, self.poll_interval = timeout, upload_wait, poll_interval

    def __repr__(self):
        return f'GeminiTranscriber(model={self.model!r}, language={self.language!r})'

    def request(self, method, url, stage, *, key=True, headers=None, **kwargs):
        headers = {**({'x-goog-api-key': self._key} if key else {}), **(headers or {})}
        try:
            response = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as error:
            raise RemoteError(f'{stage} transport failed', stage=stage, code=type(error).__name__) from None
        if response.status_code == 429:
            raise RateLimited(f'{stage} rate limited', stage=stage, retry_after=retry_after(response))
        if not 200 <= response.status_code < 300:
            raise RemoteError(f'{stage} HTTP {response.status_code}', stage=stage, code=response.status_code)
        return response

    def json(self, response, stage):
        try:
            body = response.json()
        except ValueError:
            raise RemoteError(f'{stage} response is not JSON', stage=stage, code='invalid_json') from None
        if not isinstance(body, dict):
            raise RemoteError(f'{stage} response is not an object', stage=stage, code='invalid_json')
        return body

    def upload(self, path):
        data = Path(path).read_bytes()
        start = self.request('POST', API+'/upload/v1beta/files', 'gemini_upload', json={'file': {'display_name': 'autocanvas'}},
                             headers={'X-Goog-Upload-Protocol': 'resumable', 'X-Goog-Upload-Command': 'start',
                                      'X-Goog-Upload-Header-Content-Length': str(len(data)),
                                      'X-Goog-Upload-Header-Content-Type': MIME, 'Content-Type': 'application/json'})
        url = start.headers.get('x-goog-upload-url') or ''
        if not url.startswith('https://'):
            raise RemoteError('Gemini upload URL missing', stage='gemini_upload', code='no_upload_url')
        # The upload URL is a session capability; the API key is not sent to it.
        done = self.request('POST', url, 'gemini_upload', key=False, data=data,
                            headers={'X-Goog-Upload-Offset': '0', 'X-Goog-Upload-Command': 'upload, finalize'})
        file = self.json(done, 'gemini_upload').get('file')
        if not isinstance(file, dict) or not re.fullmatch(r'files/[A-Za-z0-9_-]+', str(file.get('name') or '')):
            raise RemoteError('Gemini upload returned no file', stage='gemini_upload', code='no_file')
        return file

    def wait(self, file):
        deadline = time.monotonic()+self.upload_wait
        while file.get('state') != 'ACTIVE':
            if file.get('state') == 'FAILED':
                raise RemoteError('Gemini file processing failed', stage='gemini_file', code='FAILED')
            if time.monotonic() >= deadline:
                raise RemoteError('Gemini file not ready', stage='gemini_file', code='timeout')
            time.sleep(self.poll_interval)
            name = file['name']
            file = self.json(self.request('GET', f'{API}/v1beta/{name}', 'gemini_file'), 'gemini_file')
            file.setdefault('name', name)
        if not file.get('uri'):
            raise RemoteError('Gemini file has no URI', stage='gemini_file', code='no_uri')
        return file

    def delete(self, name):
        try:
            self.request('DELETE', f'{API}/v1beta/{name}', 'gemini_delete')
        except Exception:
            pass

    def transcribe(self, path, offset, duration):
        file = self.upload(path)
        try:
            file = self.wait(file)
            body = {'contents': [{'parts': [{'fileData': {'fileUri': file['uri'], 'mimeType': MIME}}]}],
                    'generationConfig': {'audioTranscriptionConfig': {'languageCodes': [self.language], 'wordTimestamp': True, 'mode': 'VERBATIM'}}}
            result = self.json(self.request('POST', f'{API}/v1beta/models/{self.model}:generateContent', 'gemini_generate', json=body), 'gemini_generate')
        finally:
            self.delete(file['name'])
        candidates = result.get('candidates')
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            reason = (result.get('promptFeedback') or {}).get('blockReason') if isinstance(result.get('promptFeedback'), dict) else None
            raise RemoteError('Gemini returned no transcript', stage='gemini_generate', code=reason or 'empty')
        if candidates[0].get('finishReason') in BLOCKED:
            raise RemoteError('Gemini transcript blocked', stage='gemini_generate', code=candidates[0]['finishReason'])
        if candidates[0].get('finishReason') == 'MAX_TOKENS':
            raise RemoteError('Gemini transcript truncated', stage='gemini_generate', code='MAX_TOKENS')
        return segments(result, offset, duration)
