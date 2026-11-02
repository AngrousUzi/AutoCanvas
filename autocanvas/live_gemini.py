"""Gemini Live API transcription for the live path; raw WebSocket, no execution.Inference."""
import asyncio
import base64
import json
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import aiohttp
from . import media
from .live import KeywordDetector
from .outputs import Transcript, append_event
from .types import AuthenticationRequired, MediaError, RemoteError, TranscriptSegment

LIVE_ENDPOINT = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent'


def parse_duration(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.endswith('s'):
        try:
            return float(value[:-1])
        except ValueError:
            return None
    return None


def segment_timing(info, clock_base, sent_bytes, transcript_end):
    words = info.get('words') or info.get('wordTimestamps') or []
    starts, ends = [], []
    for word in words:
        if not isinstance(word, dict):
            continue
        start = parse_duration(word.get('startOffset', word.get('start')))
        end = parse_duration(word.get('endOffset', word.get('end')))
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
    if starts and ends:
        start, end = clock_base+starts[0], clock_base+ends[-1]
    else:
        end = clock_base + sent_bytes/(2*16000)
        start = max(transcript_end, end-1.0)
    if end <= transcript_end:
        end = transcript_end+0.01
    if start >= end:
        start = max(transcript_end, end-0.01)
    return start, end


async def recv_frame(ws):
    msg = await ws.receive()
    if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
        return json.loads(msg.data)
    if msg.type == aiohttp.WSMsgType.ERROR:
        raise RemoteError('Gemini socket error')
    return None


class GeminiLiveMonitor:
    def __init__(self, resolve_sources, *, api_key, model, language='cmn-Hans-CN', queue_chunks=100, chunk_seconds=0.2,
                 keywords=(), debounce=30, reconnect_seconds=5, reader=media.audio, selector=media.select, proxy=None):
        self.resolve_sources = resolve_sources
        self.api_key, self.model, self.language = api_key, model, language
        self.queue_chunks, self.chunk_seconds = queue_chunks, chunk_seconds
        self.keywords, self.debounce = keywords, debounce
        self.reconnect_seconds = reconnect_seconds
        self.reader, self.selector = reader, selector
        self.proxy = proxy or None

    async def run(self, lecture, folder: Path, *, view=None):
        end = datetime.fromisoformat(lecture['end']).timestamp()
        if end <= time.time():
            return None
        begin = datetime.fromisoformat(lecture['begin']).timestamp()
        transcript = Transcript(folder)
        events = folder/'events.jsonl'
        queue = asyncio.Queue(maxsize=self.queue_chunks)
        detector = KeywordDetector(self.keywords, self.debounce)
        connected = False
        connect_lock = asyncio.Lock()
        stopping = asyncio.Event()
        background = set()
        conn = {'ws': None, 'handle': None}
        state = {'ever_connected': False, 'sent_bytes': 0}
        clock_base = max(0, time.time()-begin, transcript.end)
        http = aiohttp.ClientSession()
        append_event(events, {'type': 'monitor_start', 'at': time.time(), 'resume_after': transcript.end})

        async def connect():
            async with connect_lock:
                if conn['ws'] is not None and not getattr(conn['ws'], 'closed', False):
                    return conn['ws']
                try:
                    ws = await http.ws_connect(LIVE_ENDPOINT, headers={'x-goog-api-key': self.api_key}, proxy=self.proxy)
                    model = self.model if self.model.startswith('models/') else 'models/'+self.model
                    setup = {'model': model, 'generationConfig': {'responseModalities': ['TEXT']},
                             'inputAudioTranscription': {'languageCodes': [self.language], 'mode': 'VERBATIM'}}
                    if conn['handle']:
                        setup['sessionResumption'] = {'handle': conn['handle']}
                    await ws.send_json({'setup': setup})
                    while True:
                        frame = await recv_frame(ws)
                        if frame is None:
                            raise RemoteError('Gemini session closed during setup')
                        if 'setupComplete' in frame:
                            break
                except RemoteError:
                    raise
                except Exception as error:
                    raise RemoteError(f'Gemini connection failed: {type(error).__name__}') from error
                old = conn['ws']
                conn['ws'] = ws
                append_event(events, {'type': 'session_resumed' if state['ever_connected'] else 'session_connected', 'at': time.time()})
                state['ever_connected'] = True
                if old is not None:
                    try:
                        await old.close()
                    except Exception:
                        pass
                return ws

        async def reopen():
            while True:
                try:
                    return await connect()
                except RemoteError:
                    if not state['ever_connected'] or stopping.is_set() or time.time() >= end:
                        raise
                    append_event(events, {'type': 'session_expired', 'at': time.time(), 'reason': 'reconnect_failed'})
                    await asyncio.sleep(min(self.reconnect_seconds, max(0.01, end-time.time())))

        def spawn_reconnect():
            task = asyncio.ensure_future(reopen())
            background.add(task)
            task.add_done_callback(background.discard)

        def handle_final(info):
            text = (info.get('text') or '').strip()
            if not text:
                return
            start, end_ts = segment_timing(info, clock_base, state['sent_bytes'], transcript.end)
            segment = TranscriptSegment(start, end_ts, text)
            transcript.append(segment)
            for word in detector.matches(text, time.monotonic()):
                append_event(events, {'type': 'keyword', 'keyword': word, 'start': segment.start, 'text': segment.text})

        async def produce():
            nonlocal connected
            while time.time() < end:
                try:
                    async with asyncio.timeout(max(0.01, end-time.time())):
                        sources = await self.resolve_sources(lecture)
                        source = await self.selector(sources, 'audio', view)
                        offset = max(0, time.time()-begin, transcript.end)
                        reader = self.reader(source, chunk_seconds=self.chunk_seconds)
                        append_event(events, {'type': 'connected', 'at': time.time(), 'offset': offset})
                        try:
                            async for chunk in reader:
                                connected = True
                                chunk = replace(chunk, start=offset+chunk.start)
                                if queue.full():
                                    lost = queue.get_nowait()
                                    queue.task_done()
                                    append_event(events, {'type': 'audio_gap', 'reason': 'queue_full', 'start': lost.start, 'end': lost.start+lost.duration})
                                queue.put_nowait(chunk)
                        finally:
                            await reader.aclose()
                except AuthenticationRequired:
                    raise
                except Exception as error:
                    append_event(events, {'type': 'connection_interrupted', 'at': time.time(), 'error': type(error).__name__})
                remaining = end-time.time()
                if remaining > 0:
                    await asyncio.sleep(min(self.reconnect_seconds, remaining))
            await queue.put(None)

        async def send():
            ws = await reopen()
            while True:
                chunk = await queue.get()
                try:
                    if chunk is None:
                        try:
                            await ws.send_json({'realtimeInput': {'audioStreamEnd': True}})
                        except Exception:
                            pass
                        return
                    data = base64.b64encode(chunk.pcm).decode('ascii')
                    try:
                        await ws.send_json({'realtimeInput': {'audio': {'mimeType': 'audio/pcm;rate=16000', 'data': data}}})
                        state['sent_bytes'] += len(chunk.pcm)
                    except Exception:
                        ws = await reopen()
                finally:
                    queue.task_done()

        async def receive():
            ws = await reopen()
            while True:
                try:
                    frame = await asyncio.wait_for(recv_frame(ws), timeout=1.0)
                except asyncio.TimeoutError:
                    if stopping.is_set():
                        return
                    continue
                except RemoteError as error:
                    if stopping.is_set():
                        return
                    append_event(events, {'type': 'session_expired', 'at': time.time(), 'error': type(error).__name__})
                    ws = await reopen()
                    continue
                if frame is None:
                    if stopping.is_set():
                        return
                    append_event(events, {'type': 'session_expired', 'at': time.time(), 'reason': 'closed'})
                    ws = await reopen()
                    continue
                content = frame.get('serverContent')
                if content and 'inputTranscription' in content:
                    handle_final(content['inputTranscription'])
                elif 'goAway' in frame:
                    append_event(events, {'type': 'session_expired', 'at': time.time(), 'reason': 'go_away'})
                    spawn_reconnect()
                elif 'sessionResumptionUpdate' in frame:
                    update = frame['sessionResumptionUpdate']
                    if update.get('newHandle'):
                        conn['handle'] = update['newHandle']

        producer = asyncio.create_task(produce(), name='live-gemini-produce')
        sender = asyncio.create_task(send(), name='live-gemini-send')
        receiver = asyncio.create_task(receive(), name='live-gemini-receive')

        async def finish():
            await asyncio.gather(producer, sender)
            stopping.set()
            try:
                await asyncio.wait_for(asyncio.shield(receiver), 30)
            except Exception:
                pass

        finisher = asyncio.ensure_future(finish())
        try:
            await asyncio.shield(finisher)
            if not connected:
                raise MediaError('No live audio received before scheduled end')
            return transcript.finish()
        except asyncio.CancelledError:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            stopping.set()
            if not sender.done():
                if queue.full():
                    lost = queue.get_nowait()
                    queue.task_done()
                    append_event(events, {'type': 'audio_gap', 'reason': 'stop_drain', 'start': lost.start})
                queue.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(sender, receiver, return_exceptions=True)), 30)
            except Exception:
                pass
            raise
        finally:
            for task in (producer, sender, receiver, finisher):
                task.cancel()
            await asyncio.gather(producer, sender, receiver, finisher, return_exceptions=True)
            if background:
                for task in list(background):
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)
            if not queue.empty():
                append_event(events, {'type': 'audio_gap', 'reason': 'monitor_stopped', 'queued_chunks': queue.qsize()})
            if conn['ws'] is not None:
                try:
                    await conn['ws'].close()
                except Exception:
                    pass
            await http.close()
            transcript.finish()
            append_event(events, {'type': 'monitor_stop', 'at': time.time()})
