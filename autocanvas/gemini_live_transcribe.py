"""Gemini Live API transcription of recorded audio files, streamed faster than realtime."""
import asyncio
import base64
import json
import re
import time
import aiohttp
from . import media
from .types import MediaSource, RateLimited, RemoteError, TranscriptSegment

ENDPOINT = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent'
MIME = 'audio/pcm;rate=16000'
FRAME = 0.1
SESSION = 540  # wall seconds of streaming per chunk; sessions are capped at 10 minutes


def seconds(value):
    try:
        return float(value[:-1]) if isinstance(value, str) and value.endswith('s') else float(value)
    except (TypeError, ValueError):
        return None


def quota(code, reason):
    text = str(reason or '').lower()
    return code == 429 or 'resource_exhausted' in text or 'quota' in text


class Timeline:
    """Places untimed finals on voice-activity windows; times are seconds from the session's audio start."""
    def __init__(self):
        self.closed, self.open, self.claimed, self.rows = [], None, False, []

    def activity(self, kind, at):
        if at is None:
            return
        if kind == 'ACTIVITY_START':
            self.open, self.claimed = at, False
        elif kind == 'ACTIVITY_END':
            if self.claimed and self.rows:
                # The final for this window arrived before its end; correct the estimated end.
                self.rows[-1][1] = max(self.rows[-1][0], at)
            else:
                self.closed.append((at if self.open is None else self.open, at))
            self.open, self.claimed = None, False

    def final(self, text, sent):
        last = self.rows[-1][1] if self.rows else 0.0
        if self.closed:
            start, end = self.closed[0][0], self.closed[-1][1]
        elif self.open is not None:
            start, end, self.claimed = self.open, sent, True
        else:
            start, end = last, sent
        self.closed = []
        start = max(start, last)
        self.rows.append([start, max(end, start), text])


class GeminiLiveFileTranscriber:
    # Free tier: no request or daily cap, 20K tokens/min; audio is about 25 tokens/s, so roughly 13x realtime at most.
    spacing, name = 0, 'gemini-live'

    def __init__(self, api_key, model='gemini-3.5-transcribe-live', language='cmn-Hans-CN', speed=4, idle=15, setup_timeout=30, reader=media.audio, proxy=None):
        if not api_key:
            raise ValueError('Gemini API key required')
        if not re.fullmatch(r'[A-Za-z0-9._/-]+', model or ''):
            raise ValueError('Invalid Gemini model name')
        if type(speed) not in (int, float) or not 1 <= speed <= 6:
            raise ValueError('Gemini Live speed must be between 1 and 6')
        self._key, self.language, self.speed = api_key, language, speed
        self.model = model if model.startswith('models/') else 'models/'+model
        self.idle, self.setup_timeout, self.reader = idle, setup_timeout, reader
        self.proxy = proxy or None

    def __repr__(self):
        return f'GeminiLiveFileTranscriber(model={self.model!r}, language={self.language!r}, speed={self.speed!r})'

    def failure(self, message, code, reason=None):
        if quota(code, reason):
            return RateLimited('gemini_live rate limited', stage='gemini_live', retry_after=60)
        reason = str(reason or '').replace(self._key, '***')[:200]
        return RemoteError(f'gemini_live {message} {code}' + (f': {reason}' if reason else ''), stage='gemini_live', code=code)

    async def message(self, ws, timeout):
        msg = await asyncio.wait_for(ws.receive(), timeout)
        if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            try:
                frame = json.loads(msg.data)
            except ValueError:
                raise RemoteError('gemini_live frame is not JSON', stage='gemini_live', code='invalid_json') from None
            if isinstance(frame, dict) and isinstance(frame.get('error'), dict):
                error = frame['error']
                raise self.failure('error', error.get('code') or error.get('status'), f"{error.get('status') or ''} {error.get('message') or ''}")
            return frame if isinstance(frame, dict) else {}
        if msg.type == aiohttp.WSMsgType.ERROR:
            raise RemoteError('gemini_live socket error', stage='gemini_live', code=type(msg.data).__name__)
        return (msg.data, msg.extra) if msg.type == aiohttp.WSMsgType.CLOSE else (getattr(ws, 'close_code', None), None)

    async def connect(self, http):
        try:
            ws = await asyncio.wait_for(http.ws_connect(ENDPOINT, headers={'x-goog-api-key': self._key}, proxy=self.proxy), self.setup_timeout)
        except aiohttp.WSServerHandshakeError as error:
            raise self.failure('handshake HTTP', error.status) from None
        except (aiohttp.ClientError, OSError) as error:
            raise RemoteError('gemini_live connection failed', stage='gemini_live', code=type(error).__name__) from None
        try:
            await ws.send_json({'setup': {'model': self.model, 'generationConfig': {'responseModalities': ['TEXT']},
                                          'inputAudioTranscription': {'languageCodes': [self.language], 'mode': 'VERBATIM'}}})
            while True:
                frame = await self.message(ws, self.setup_timeout)
                if isinstance(frame, tuple):
                    raise self.failure('closed during setup', *frame)
                if 'setupComplete' in frame:
                    return ws
        except BaseException as error:
            await ws.close()
            if isinstance(error, (aiohttp.ClientError, OSError)):
                raise RemoteError('gemini_live setup failed', stage='gemini_live', code=type(error).__name__) from None
            raise

    async def send(self, ws, path, ended, sent):
        reader = self.reader(MediaSource(str(path)), chunk_seconds=FRAME)
        began = time.monotonic()
        try:
            async for chunk in reader:
                sent[0] += chunk.duration
                await ws.send_json({'realtimeInput': {'audio': {'mimeType': MIME, 'data': base64.b64encode(chunk.pcm).decode('ascii')}}})
                # Pace against a deadline, not per-frame sleeps that timer granularity stretches; catch up at most 1 s after a stall.
                began = max(began, time.monotonic()-sent[0]/self.speed-1)
                await asyncio.sleep(max(0, began+sent[0]/self.speed-time.monotonic()))
        finally:
            await reader.aclose()
        await ws.send_json({'realtimeInput': {'audioStreamEnd': True}})
        ended.set()

    async def receive(self, ws, timeline, ended, sent):
        while True:
            idle = ended.is_set()
            try:
                frame = await self.message(ws, self.idle if idle else min(1, self.idle))
            except asyncio.TimeoutError:
                if idle:
                    return
                continue
            if isinstance(frame, tuple):
                if ended.is_set() and not quota(*frame):
                    return
                raise self.failure('closed', *frame)
            content = frame.get('serverContent') if isinstance(frame.get('serverContent'), dict) else {}
            activity = frame.get('voiceActivity') or content.get('voiceActivity')
            if isinstance(activity, dict):
                timeline.activity(activity.get('type') or activity.get('voiceActivityType'), seconds(activity.get('audioOffset')))
            final = content.get('inputTranscription')
            text = str(final.get('text') or '').strip() if isinstance(final, dict) else ''
            if text:
                timeline.final(text, sent[0])
            if content.get('turnComplete') and ended.is_set():
                return

    async def transcribe(self, path, offset, duration):
        if duration/self.speed > SESSION:
            raise ValueError(f'Gemini Live chunk needs {duration/self.speed:.0f}s of streaming; at most {SESSION}s fits a session')
        timeline, ended, sent = Timeline(), asyncio.Event(), [0.0]
        http = aiohttp.ClientSession()
        try:
            ws = await self.connect(http)
            tasks = [asyncio.create_task(self.receive(ws, timeline, ended, sent)), asyncio.create_task(self.send(ws, path, ended, sent))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
                if not tasks[0].done():
                    # A failed send usually means the socket closed; the receiver reports why.
                    await asyncio.wait(tasks[:1], timeout=2)
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()
            except (aiohttp.ClientError, OSError) as error:
                raise RemoteError('gemini_live socket failed', stage='gemini_live', code=type(error).__name__) from None
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                try:
                    await ws.close()
                except Exception:
                    pass
        finally:
            await http.close()
        clamp = lambda t: min(max(offset+t, offset), offset+duration)
        return [TranscriptSegment(clamp(start), clamp(end), text) for start, end, text in timeline.rows]
