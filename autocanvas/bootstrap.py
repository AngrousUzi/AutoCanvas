"""Composition root; the only place that assembles concrete module dependencies."""
import os
from contextlib import contextmanager
from .auth import Auth
from .canvas import Canvas
from .video import Video
from .storage import Store
from .asr import Recognizer
from .execution import Inference, blocking
from .flows import CatalogSync, AssignmentSync, Replay
from .live import LiveMonitor
from .live_gemini import GeminiLiveMonitor
from .gemini_transcribe import GeminiTranscriber
from .gemini_live_transcribe import GeminiLiveFileTranscriber
from .groq_transcribe import GroqTranscriber, PROMPT
from .service import Service


@contextmanager
def service_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root/'service.lock').open('a') as handle:
        try:
            if os.name == 'nt':
                import msvcrt
                if handle.tell() == 0:
                    handle.write('\0')
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('Another service owns this runtime; use its HTTP API') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def replay_asr():
    # Replay ASR backend is independent of the live AUTOCANVAS_ASR_BACKEND; remote backends are listed in priority order.
    raw = os.environ.get('AUTOCANVAS_REPLAY_ASR_BACKEND') or 'local'
    names = [n.strip().lower() for n in raw.split(',')]
    if names == ['local']:
        return {}
    if len(set(names)) != len(names) or not set(names) <= {'gemini-live', 'gemini', 'groq'}:
        raise ValueError(f'Invalid AUTOCANVAS_REPLAY_ASR_BACKEND: {raw} (local, or a comma list of gemini-live, gemini and groq)')
    try:
        chunk = float(os.environ.get('AUTOCANVAS_REPLAY_CHUNK_SECONDS', '1500'))
    except ValueError:
        raise ValueError('AUTOCANVAS_REPLAY_CHUNK_SECONDS must be a number') from None
    if not 60 <= chunk <= 1680:
        raise ValueError('AUTOCANVAS_REPLAY_CHUNK_SECONDS must be between 60 and 1680')
    transcribers = []
    proxy = os.environ.get('AUTOCANVAS_ASR_PROXY') or None
    language = os.environ.get('AUTOCANVAS_GEMINI_REPLAY_LANGUAGE') or 'cmn-Hans-CN'
    for name in names:
        if name.startswith('gemini'):
            api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
            if not api_key:
                raise ValueError(f'GEMINI_API_KEY or GOOGLE_API_KEY must be set for the {name} replay ASR backend')
        if name == 'gemini-live':
            try:
                speed = float(os.environ.get('AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED') or '4')
            except ValueError:
                raise ValueError('AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED must be a number') from None
            # Each chunk streams in one Live session, capped at 10 minutes.
            if not 1 <= speed <= 6 or chunk/speed > 540:
                raise ValueError('AUTOCANVAS_GEMINI_REPLAY_LIVE_SPEED must be between 1 and 6, and AUTOCANVAS_REPLAY_CHUNK_SECONDS/speed at most 540')
            transcribers.append(GeminiLiveFileTranscriber(api_key, model=os.environ.get('AUTOCANVAS_GEMINI_REPLAY_LIVE_MODEL') or 'gemini-3.5-transcribe-live',
                                                          language=language, speed=speed, proxy=proxy))
        elif name == 'gemini':
            transcribers.append(GeminiTranscriber(api_key, model=os.environ.get('AUTOCANVAS_GEMINI_REPLAY_MODEL') or 'gemini-3.5-transcribe', language=language, proxy=proxy))
        else:
            api_key = os.environ.get('GROQ_API_KEY')
            if not api_key:
                raise ValueError('GROQ_API_KEY must be set for the groq replay ASR backend')
            transcribers.append(GroqTranscriber(api_key, model=os.environ.get('AUTOCANVAS_GROQ_MODEL') or 'whisper-large-v3-turbo',
                                                language=os.environ.get('AUTOCANVAS_GROQ_LANGUAGE') or 'zh',
                                                prompt=os.environ.get('AUTOCANVAS_GROQ_PROMPT') or PROMPT, proxy=proxy))
    return {'transcribers': transcribers, 'remote_chunk_seconds': chunk}


def build(settings):
    settings.validate()
    auth = Auth(settings.root/'auth'/'canvas_session.json')
    canvas = Canvas(auth.session)
    store = Store(settings.root/'state.sqlite3')
    catalog = CatalogSync(canvas, lambda course: Video(auth.video(course)), store, settings.course_ids)
    assignments = AssignmentSync(canvas, auth.session, store, settings.root/'assignments')
    inference = Inference(Recognizer(settings.model, settings.device).transcribe)

    async def resolve(lecture):
        return await blocking(catalog.sources, lecture)

    async def platform_transcript(lecture):
        return await blocking(catalog.transcript, lecture)

    replay = Replay(resolve, inference.transcribe, settings.root/'outputs', settings.root/'cache', chunk_seconds=settings.chunk_seconds,
                    sample_every=settings.sample_every, platform_transcript=platform_transcript, **replay_asr())

    backend = os.environ.get('AUTOCANVAS_ASR_BACKEND', 'local')
    if backend == 'gemini':
        api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if not api_key:
            raise ValueError('GEMINI_API_KEY or GOOGLE_API_KEY must be set for the gemini ASR backend')
        model = os.environ.get('AUTOCANVAS_GEMINI_MODEL')
        if not model:
            raise ValueError('AUTOCANVAS_GEMINI_MODEL must name a Live API transcription model')
        monitor = GeminiLiveMonitor(resolve, api_key=api_key, model=model,
                                    language=os.environ.get('AUTOCANVAS_GEMINI_LANGUAGE', 'cmn-Hans-CN'),
                                    queue_chunks=settings.live_queue_chunks, chunk_seconds=settings.live_chunk_seconds,
                                    keywords=settings.keywords, debounce=settings.keyword_debounce,
                                    proxy=os.environ.get('AUTOCANVAS_ASR_PROXY') or None)
    elif backend == 'local':
        monitor = LiveMonitor(resolve, inference.transcribe, queue_chunks=settings.live_queue_chunks, chunk_seconds=settings.chunk_seconds,
                              keywords=settings.keywords, debounce=settings.keyword_debounce)
    else:
        raise ValueError(f'Unknown AUTOCANVAS_ASR_BACKEND: {backend}')

    service = Service(settings, store, catalog, assignments, replay, monitor)
    from .configuration import Configuration
    from .browser_auth import BrowserAuth

    def apply_idle(values, changed):
        if changed & {'model', 'device'}:
            inference.recognize = Recognizer(values['model'], values['device']).transcribe
        replay.chunk_seconds, replay.sample_every = values['chunk_seconds'], values['sample_every']
        if isinstance(service.monitor, GeminiLiveMonitor):
            # Keep the configured live backend; only its tunables change.
            service.monitor.queue_chunks, service.monitor.keywords, service.monitor.debounce = values['live_queue_chunks'], values['keywords'], values['keyword_debounce']
            return
        service.monitor = LiveMonitor(resolve, inference.transcribe, queue_chunks=values['live_queue_chunks'],
                                      chunk_seconds=values['chunk_seconds'], keywords=values['keywords'], debounce=values['keyword_debounce'])

    def apply_hot():
        catalog.course_ids = set(settings.course_ids)
        for course in store.list('courses'):
            course['active'] = not settings.course_ids or course['id'] in settings.course_ids
            store.put('courses', course['id'], course)

    service.configuration = Configuration(settings, lambda: bool(service.active) or inference.busy or not inference.queue.empty(), apply_idle, apply_hot)
    from .attendance import MobileLogin
    from .live_attendance import LiveAttendance
    async def resolve_attendance(lecture, protocol):
        return await blocking(catalog.sources, lecture, live_protocol=protocol)
    service.attendance = LiveAttendance(store, settings.root, resolve_attendance, MobileLogin(auth.session))
    service.browser_auth = BrowserAuth(auth)
    return service, inference
