"""Live-only attendance: strict QR parsing, latest-value dispatch and independent OAuth."""
import asyncio
import hashlib
import re
import time
from urllib.parse import parse_qs, urlsplit
import aiohttp
from .execution import blocking
from .types import AuthenticationRequired

HOST = 'https://mlearning.sjtu.edu.cn'
MAX_AGE = 2.0  # local receive-to-dispatch budget; upstream stream delay is separate
SIGN_DELAY = 3.0  # wait from first detection, then select a fresh frame


def parse_code(text, course_id):
    """Never navigate an arbitrary decoded URL or accept replay data as an instruction."""
    url = urlsplit(text)
    if (url.scheme != 'https' or url.netloc != 'mlearning.sjtu.edu.cn'
            or url.path not in ('/lms/mobile2/forscan/', '/lms/mobile2/forscan')):
        return None
    q = parse_qs(url.query)
    if any(len(q.get(k, [])) != 1 for k in ('courseCode', 'rollCallToken', 'signHistoryId')):
        return None
    course, token, history = (q[k][0] for k in ('courseCode', 'rollCallToken', 'signHistoryId'))
    if course != str(course_id) or not course.isdigit():
        return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', token) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', history):
        return None
    return {'course_id': course, 'token': token, 'history': history,
            'fingerprint': hashlib.sha256(token.encode()).hexdigest()[:20], 'key': course+':'+history}


class MobileLogin:
    """Dedicated mobile token; shares only the existing jAccount session factory."""
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.token = None
        self.refreshed = 0
        self.lock = asyncio.Lock()

    def login(self):
        with self.session_factory() as session:
            response = session.get('https://jaccount.sjtu.edu.cn/oauth2/authorize', params={
                'client_id': 'pmZTZ9QZEAwXGLGheU99', 'response_type': 'code', 'scope': 'essential',
                'redirect_uri': HOST+'/lms-auth-sjtu/auth/oauth2/mobile/callback'}, timeout=15)
            if urlsplit(response.url).hostname != 'mlearning.sjtu.edu.cn':
                raise AuthenticationRequired('移动课堂需要重新登录')
            token = session.cookies.get('token', domain='mlearning.sjtu.edu.cn')
            if not token:
                raise AuthenticationRequired('移动课堂未返回登录凭据')
            return token

    async def prepare(self):
        async with self.lock:
            if not self.token or time.monotonic()-self.refreshed > 1200:
                self.token = await blocking(self.login)
                self.refreshed = time.monotonic()
        return self.token

    async def submit(self, code):
        # No redirect, automatic retry, cookie refresh, or arbitrary QR navigation here.
        # A broken connection may already have reached the server: record uncertainty.
        url = HOST+'/lms-lti-rollcall-sjtu/sign/scan/'+code['token']+'/'+code['history']
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True),
                                        timeout=aiohttp.ClientTimeout(total=4)) as client:
            async with client.get(url, headers={'Authorization': self.token}, allow_redirects=False) as response:
                if response.status in (401, 403):
                    self.token = None
                    return {'status': 'needs_login', 'message': '移动课堂登录已失效，等待最新画面重新认证'}
                if response.status != 200:
                    return {'status': 'unknown', 'message': '签到响应异常，请核对学校签到记录'}
                data = await response.json()
        code_value = str(data.get('resultCode'))
        body = data.get('body')
        message = str(data.get('resultMessage') or '')
        # Do not persist server messages which can echo the full secret QR token.
        if code_value in ('10001', '10005'):
            self.token = None
            return {'status': 'needs_login', 'message': '移动课堂登录已失效'}
        if any(word in message.lower() for word in ('过期', '不存在', 'expired')):
            return {'status': 'expired', 'message': '二维码已过期或失效，等待新码'}
        location_notice = any(word in message.lower() for word in ('定位', '位置', '范围', 'gps', 'location'))
        if code_value != '200':
            if location_notice:
                return {'status': 'needs_action', 'message': '已尝试扫码签到，但学校未确认成功并提示定位，请核对签到记录', 'location_reminder': True}
            return {'status': 'rejected', 'message': '学校拒绝签到（响应码 '+code_value+'），等待新码或人工检查'}
        state = str(body.get('status', '')) if isinstance(body, dict) else ''
        if state in ('EXPIRED', 'UNKNOWN'):
            return {'status': 'expired', 'message': '二维码已过期或无效'}
        if state and state not in ('NORMAL', 'EXISTED'):
            return {'status': 'needs_action', 'message': '学校返回额外签到状态，请检查签到记录'}
        result = {'status': 'succeeded', 'message': '学校签到接口已确认成功'}
        if location_notice:
            result.update(location_reminder=True, message='学校签到接口已确认成功；另有定位提示，可在交我办核对定位要求')
        return result


class LatestAttendance:
    """One decoded mailbox per live view. Never queue codes behind authentication/I/O."""
    def __init__(self, client, store, enabled, *, sign_delay=SIGN_DELAY):
        self.client, self.store, self.enabled = client, store, enabled
        self.latest = {}
        self.first_seen = {}
        self.sign_delay = sign_delay
        self.revision = 0
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.auth_status = 'idle'

    def offer(self, view, rows, course, lecture, received, image=''):
        self.revision += 1
        codes = [parse_code(row['content'], course) for row in rows]
        codes = [c for c in codes if c]
        for code in codes:
            self.first_seen.setdefault(code['key'], time.monotonic())
        self.latest[view] = [dict(c, lecture_id=lecture, received=received, image=image,
                                  revision=self.revision) for c in codes]
        self.changed.set()

    def fresh(self):
        now = time.monotonic()
        candidates = [c for rows in self.latest.values() for c in rows
                      if self.enabled(c['course_id']) and 0 <= now-c['received'] <= MAX_AGE]
        for c in sorted(candidates, key=lambda r: r['received'], reverse=True):
            old = self.store.get('attendance', c['key'], {})
            if old.get('status') in ('succeeded', 'submitting', 'unknown', 'needs_action'):
                continue
            if old.get('fingerprint') == c['fingerprint']:
                if old.get('status') != 'needs_login' or time.time()-old.get('at', 0) < 30:
                    continue
            return c
        return None

    async def dispatch(self):
        async with self.lock:
            if not self.fresh():
                return
            self.auth_status = 'authenticating'
            try:
                await self.client.prepare()
                self.auth_status = 'ready'
            except Exception as error:
                self.auth_status = 'needs_login' if isinstance(error, AuthenticationRequired) else 'unavailable'
                await asyncio.sleep(3)
                return
            # Authentication and the intentional delay can outlive a QR token.
            # Re-select after every wait, including when another rollcall appears.
            while True:
                code = self.fresh()
                if not code:
                    return
                remaining = self.first_seen[code['key']] + self.sign_delay - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            record = {k: code[k] for k in ('course_id', 'lecture_id', 'history', 'fingerprint', 'image')}
            record.update(id=code['key'], status='submitting', at=time.time(),
                          age_ms=round((time.monotonic()-code['received'])*1000),
                          message='正在使用最新已解码直播画面签到')
            self.store.put('attendance', code['key'], record)
            started = time.monotonic()
            try:
                result = await self.client.submit(code)
            except asyncio.CancelledError:
                record.update(status='unknown', message='请求中断，学校可能已处理，请人工核对')
                self.store.put('attendance', code['key'], record)
                raise
            except Exception:
                result = {'status': 'unknown', 'message': '网络结果不确定，停止自动重发，请核对学校签到记录'}
            record.update(result, request_ms=round((time.monotonic()-started)*1000))
            self.store.put('attendance', code['key'], record)

    async def run(self):
        # Requests pending across process restart must not be blindly replayed.
        for row in self.store.list('attendance'):
            if row['status'] == 'submitting':
                row.update(status='unknown', message='服务重启前请求未完成，请核对学校签到记录')
                self.store.put('attendance', row['id'], row)
        while True:
            await self.changed.wait()
            self.changed.clear()
            await self.dispatch()
