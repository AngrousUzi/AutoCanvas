"""Explicit application use cases. Leaf modules never import this module."""
import asyncio
import inspect
import json
import logging
import shutil
import time
from pathlib import Path
from . import media
from .execution import blocking
from .outputs import Transcript, append_event, atomic_json, transcript_text
from .types import AuthenticationRequired, MediaError, RateLimited

log = logging.getLogger(__name__)


def lecture_key(course_id, kind, lecture_id):
    return f'{course_id}:{kind}:{lecture_id}'


class CatalogSync:
    def __init__(self, canvas, video_factory, store, course_ids=()):
        self.canvas, self.video_factory, self.store = canvas, video_factory, store
        self.course_ids = set(course_ids)

    def courses(self):
        courses = self.canvas.courses()
        for course in courses:
            course['active'] = not self.course_ids or course['id'] in self.course_ids
            self.store.put('courses', course['id'], course)
        current = {c['id'] for c in courses}
        for old in self.store.list('courses'):
            if old['id'] not in current:
                old['active'] = False
                self.store.put('courses', old['id'], old)
        return courses

    def videos(self, course_id):
        client = self.video_factory(course_id)
        try:
            teaching_class = client.context()
            result = []
            for kind in ('vod', 'live'):
                rows = client.lectures(course_id, teaching_class, kind)
                for row in rows:
                    self.store.put('lectures', lecture_key(course_id, kind, row['id']), row)
                result.extend(rows)
            return result
        finally:
            client.close()

    def sources(self, lecture, *, live_protocol=3):
        # Each operation gets fresh course-scoped credentials. Nothing is cached in the DB.
        for attempt in range(2):
            client = self.video_factory(lecture['course_id'])
            try:
                if lecture['kind'] == 'live' and live_protocol != 3:
                    return client.sources(lecture['id'], lecture['kind'], live_protocol=live_protocol)
                return client.sources(lecture['id'], lecture['kind'])
            except AuthenticationRequired:
                if attempt:
                    raise
            finally:
                client.close()

    def transcript(self, lecture):
        for attempt in range(2):
            client = self.video_factory(lecture['course_id'])
            try:
                return client.transcript(lecture['id'])
            except AuthenticationRequired:
                if attempt:
                    raise
            finally:
                client.close()


class AssignmentSync:
    def __init__(self, canvas, session_factory, store, root):
        self.canvas, self.session_factory, self.store, self.root = canvas, session_factory, store, root

    def run(self, course_id):
        from .assignments import export
        rows = self.canvas.assignments(course_id)
        failed = False
        with self.session_factory() as session:
            for row in rows:
                result = export(session, row, self.root/str(course_id)/str(row['id']))
                result['course_id'] = str(course_id)
                self.store.put('assignments', f"{course_id}:{row['id']}", result)
                failed |= any(a['status'] == 'failed' for a in result['attachments'])
        if failed:
            raise IOError('Some attachments failed; successful attachments retained')
        return self.root/str(course_id)


async def transcribe_source(source, transcribe, folder, *, chunk_seconds=3, duration=None):
    output = Transcript(folder)
    reader = media.audio(source, offset=output.end, duration=max(0, duration-output.end) if duration else None, chunk_seconds=chunk_seconds)
    try:
        async for chunk in reader:
            output.append(await transcribe(chunk))
    finally:
        await reader.aclose()
        output.finish()
    return folder/'transcript.json'


async def remote_chunk(transcribers, path, offset, real):
    # Pacing and quota state live on the transcriber instances, so they persist across jobs.
    for t in transcribers:
        t.next_at, t.blocked_until = getattr(t, 'next_at', 0.0), getattr(t, 'blocked_until', 0.0)
    for t in transcribers:
        if t.blocked_until > time.monotonic():
            continue
        for attempt in range(5):
            await asyncio.sleep(max(0, t.next_at-time.monotonic()))
            try:
                if inspect.iscoroutinefunction(t.transcribe):
                    return t, await t.transcribe(path, offset, real)
                return t, await blocking(t.transcribe, path, offset, real)
            except RateLimited as error:
                wait = error.retry_after
            finally:
                t.next_at = time.monotonic()+t.spacing
            if attempt == 4 or (wait is not None and wait > 120):
                break
            await asyncio.sleep(wait if wait is not None else 2**attempt)
        wait = 3600 if wait is None else wait
        t.blocked_until = time.monotonic()+wait
        log.warning('%s transcription rate limited; blocked for %.0fs', t.name, wait)
    wait = min(t.blocked_until for t in transcribers)-time.monotonic() if transcribers else None
    raise RateLimited('All remote transcribers rate limited', stage='transcribe', retry_after=max(0, wait) if wait is not None else None)


async def transcribe_source_remote(source, transcribers, folder, cache, *, chunk_seconds=1500, duration=None):
    output = Transcript(folder)
    cache.mkdir(parents=True, exist_ok=True)
    offset = output.end
    try:
        while True:
            length = min(chunk_seconds, duration-offset) if duration else chunk_seconds
            if length < 0.5:
                break
            path = cache/f'{int(offset*1000):012d}.mp3'
            try:
                real = await media.audio_file(source, path, offset=offset, duration=length)
                if real < 0.5:
                    break
                t, rows = await remote_chunk(transcribers, path, offset, real)
                for segment in rows:
                    output.append(segment)
                append_event(folder/'events.jsonl', {'type': 'chunk', 'start': offset, 'end': offset+real, 'backend': t.name, 'at': time.time()})
            finally:
                path.unlink(missing_ok=True)
            offset += real
            if real < length-1:
                break
    finally:
        output.finish()
        shutil.rmtree(cache, ignore_errors=True)
    return folder/'transcript.json'


def save_platform_transcript(data, folder):
    rows = []
    for key in ('beforeAssemblyList', 'afterAssemblyList'):
        items = data.get(key) if isinstance(data, dict) else None
        for item in items if isinstance(items, list) else []:
            try:
                text = str(item.get('res') or '').strip()
                start, end = float(item['bg'])/1000, float(item['ed'])/1000
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            if text:
                rows.append({'start': start, 'end': max(start, end), 'text': text})
        if rows:
            break
    if not rows:
        return None
    rows.sort(key=lambda r: r['start'])
    atomic_json(folder/'raw.json', data)
    atomic_json(folder/'transcript.json', rows)
    tmp = folder/'transcript.txt.tmp'
    tmp.write_text(transcript_text(rows), encoding='utf-8')
    tmp.replace(folder/'transcript.txt')
    return folder/'transcript.json'


async def slides_source(source, folder, cache, *, sample_every=5, duration=None):
    from .slides import extract
    if cache.exists():
        shutil.rmtree(cache)
    cache.mkdir(parents=True)
    pending = folder/'pending'
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True, exist_ok=True)
    try:
        await media.sample_frames(source, cache, every=sample_every, duration=duration)
        rows = await blocking(extract, cache, pending, sample_every=sample_every)
        serial = []
        for row in rows:
            row = dict(row)
            row['image'] = Path(row['image']).name
            row.pop('frame', None)
            serial.append(row)
        atomic_json(pending/'slides.json', serial)
        # Optional analysis is isolated: a QR failure must not discard extracted slides.
        from .qr import scan_images
        try:
            qr_report = await blocking(scan_images, pending, serial)
        except Exception as error:
            qr_report = {'version': 1, 'status': 'failed', 'events': [], 'error': type(error).__name__}
        atomic_json(pending/'qr.json', qr_report)
        target = folder/'result'
        backup = folder/'previous'
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.rename(backup)
        pending.rename(target)
        if backup.exists():
            shutil.rmtree(backup)
        return target/'slides.json'
    finally:
        shutil.rmtree(cache, ignore_errors=True)


class Replay:
    def __init__(self, resolve_sources, transcribe, root, cache, *, chunk_seconds=3, sample_every=5, platform_transcript=None,
                 transcribers=(), remote_chunk_seconds=1500):
        self.resolve_sources, self.transcribe = resolve_sources, transcribe
        self.root, self.cache = root, cache
        self.chunk_seconds, self.sample_every = chunk_seconds, sample_every
        self.platform_transcript, self.transcribers, self.remote_chunk_seconds = platform_transcript, list(transcribers), remote_chunk_seconds

    async def transcribe_media(self, source, folder, cache, duration=None):
        if self.transcribers:
            return await transcribe_source_remote(source, self.transcribers, folder, cache, chunk_seconds=self.remote_chunk_seconds, duration=duration)
        return await transcribe_source(source, self.transcribe, folder, chunk_seconds=self.chunk_seconds, duration=duration)

    async def run(self, lecture, kind, options):
        if kind == 'vod_asr' and self.platform_transcript is not None:
            # Optional platform copy is isolated: it must never fail or block replay ASR.
            try:
                save_platform_transcript(await self.platform_transcript(lecture), self.root/lecture['course_id']/lecture['id']/'platform')
            except Exception as error:
                log.warning('Platform transcript skipped: %s', type(error).__name__)
        sources = await self.resolve_sources(lecture)
        purpose = 'audio' if kind == 'vod_asr' else 'screen'
        source = await media.select(sources, purpose, options.get('view'))
        folder = self.root/lecture['course_id']/lecture['id']/kind
        if kind == 'vod_asr':
            return await self.transcribe_media(source, folder, self.cache/'asr'/lecture['course_id']/lecture['id'], duration=options.get('duration'))
        return await slides_source(source, folder, self.cache/lecture['course_id']/lecture['id'], sample_every=self.sample_every, duration=options.get('duration'))
