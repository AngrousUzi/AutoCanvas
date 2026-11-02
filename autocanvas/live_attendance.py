"""Live video observer; independent of audio inference, replay and HTTP UI."""
import asyncio
import hashlib
import time
from datetime import datetime
from . import media
from .qr import QRDetector
from .execution import blocking
from .attendance import LatestAttendance


class LiveAttendance:
    def __init__(self, store, root, resolve, client, *, reader=media.live_frames, detector_factory=QRDetector):
        self.store, self.root, self.resolve = store, root, resolve
        self.reader, self.detector_factory = reader, detector_factory
        self.worker = LatestAttendance(client, store, self.enabled)
        self.tasks, self.views = {}, {}
        self.last_error = ''

    def config(self):
        return self.store.get('control', 'attendance', {'enabled': False, 'course_ids': []})

    def configure(self, body):
        if set(body) != {'enabled', 'course_ids'} or type(body['enabled']) is not bool:
            raise ValueError('需要 enabled 和 course_ids')
        if not isinstance(body['course_ids'], list) or any(not isinstance(c, str) or not c.isdigit() for c in body['course_ids']):
            raise ValueError('无效课程列表')
        self.store.put('control', 'attendance', body)
        # Clear pending codes immediately, including disable followed by rapid re-enable.
        self.worker.latest.clear()
        return self.status()

    def enabled(self, course):
        config = self.config()
        return (config['enabled'] and (not config['course_ids'] or course in config['course_ids'])
                and self.store.get('courses', course, {}).get('active', False)
                and not self.store.get('control', 'automation', {}).get('paused', False))

    def status(self):
        return {'config': self.config(), 'auth_status': self.worker.auth_status,
                'views': list(self.views.values()), 'error': self.last_error,
                'records': sorted(self.store.list('attendance'), key=lambda r:r['at'], reverse=True)[:300],
                'sample_seconds': .5, 'max_local_age_seconds': 2,
                'automation_paused': self.store.get('control', 'automation', {}).get('paused', False)}

    async def observe(self, lecture, source, index):
        key = f"{lecture['course_id']}:{lecture['id']}:{index}"
        detector = self.detector_factory()
        slot = None
        sequence = 0
        changed = asyncio.Event()
        state = {'course_id': lecture['course_id'], 'lecture_id': lecture['id'], 'view': str(source.view),
                 'last_frame': None, 'last_decode': None, 'decode_ms': None, 'superseded': 0, 'status': 'connecting'}
        self.views[key] = state
        folder = self.root/'outputs'/lecture['course_id']/lecture['id']/'live'/'attendance'
        folder.mkdir(parents=True, exist_ok=True)

        async def capture():
            nonlocal slot, sequence
            reader = self.reader(source)
            try:
                async for data in reader:
                    sequence += 1
                    slot = (sequence, time.monotonic(), data)
                    self.worker.latest.pop(key, None)  # newer image invalidates old decoded payload immediately
                    state.update(last_frame=time.time(), status='watching')
                    self.last_error = ''
                    changed.set()
            finally:
                await reader.aclose()

        async def decode():
            while True:
                await changed.wait()
                changed.clear()
                seq, received, data = slot
                start = time.monotonic()
                rows = await blocking(detector.detect_bytes, data)
                state.update(last_decode=time.time(), decode_ms=round((time.monotonic()-start)*1000))
                if seq != sequence:
                    state['superseded'] += 1
                    continue
                image = ''
                if rows:
                    name = hashlib.sha256('\n'.join(r['content'] for r in rows).encode()).hexdigest()[:20]+'.jpg'
                    target = folder/name
                    # Only persist evidence for recognized attendance links, not arbitrary QR contents.
                    from .attendance import parse_code
                    if any(parse_code(r['content'], lecture['course_id']) for r in rows):
                        if not target.exists():target.write_bytes(data)
                        image = target.relative_to(self.root).as_posix()
                self.worker.offer(key, rows, lecture['course_id'], lecture['id'], received, image)

        tasks = [asyncio.create_task(capture()), asyncio.create_task(decode())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.worker.latest.pop(key, None)
            self.views.pop(key, None)

    async def lecture(self, lecture):
        protocol = 2  # Prefer HTTP-FLV; fall back to latest HLS segment on connection failure.
        while True:
            tasks = []
            try:
                sources = await self.resolve(lecture, protocol)
                if not sources:raise ValueError('No live sources')
                tasks = [asyncio.create_task(self.observe(lecture, s, i)) for i,s in enumerate(sources)]
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = '直播扫码连接或解码异常，正在切换画面协议重连：'+type(error).__name__
                protocol = 3 if protocol == 2 else 2
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(2)

    async def run(self):
        dispatcher = asyncio.create_task(self.worker.run())
        warm = None
        warmed_at = 0
        async def prepare():
            try:
                self.worker.auth_status = 'authenticating'
                await self.worker.client.prepare()
                self.worker.auth_status = 'ready'
            except Exception:
                self.worker.auth_status = 'needs_login'
        try:
            while True:
                if dispatcher.done():
                    await dispatcher
                now = time.time()
                if self.config()['enabled'] and now-warmed_at > 60 and (warm is None or warm.done()):
                    warmed_at = now
                    warm = asyncio.create_task(prepare())
                wanted = {}
                for lecture in self.store.list('lectures'):
                    try:
                        if (lecture.get('kind') == 'live' and self.enabled(lecture['course_id'])
                                and datetime.fromisoformat(lecture['begin']).timestamp() <= now < datetime.fromisoformat(lecture['end']).timestamp()):
                            wanted[lecture['course_id']+':'+lecture['id']] = lecture
                    except (ValueError, KeyError):continue
                for key in list(self.tasks):
                    if key not in wanted:
                        self.tasks[key].cancel()
                        await asyncio.gather(self.tasks.pop(key), return_exceptions=True)
                for key, lecture in wanted.items():
                    if key not in self.tasks:
                        self.tasks[key] = asyncio.create_task(self.lecture(lecture))
                await asyncio.sleep(1)
        finally:
            if warm:
                warm.cancel()
                await asyncio.gather(warm, return_exceptions=True)
            for task in [dispatcher, *self.tasks.values()]:task.cancel()
            await asyncio.gather(dispatcher, *self.tasks.values(), return_exceptions=True)
            self.tasks.clear()
