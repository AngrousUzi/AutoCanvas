"""Query the same Codex CLI used for execution; never treat its disk cache as current."""
import asyncio
import json


async def list_models(executable, cwd, *, timeout=20):
    proc = await asyncio.create_subprocess_exec(
        executable, 'app-server', '--listen', 'stdio://', cwd=cwd,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=4*1024*1024)
    async def send(value):
        proc.stdin.write((json.dumps(value)+'\n').encode())
        await proc.stdin.drain()
    async def call(number, method, params):
        await send({'id': number, 'method': method, 'params': params})
        while True:
            line = await proc.stdout.readline()
            if not line:raise ValueError('Codex 模型查询提前结束，请检查 CLI 配置和登录状态')
            row = json.loads(line)
            if row.get('id') != number:continue
            if 'error' in row:raise ValueError('Codex 拒绝模型查询，请检查 CLI 版本、配置和登录状态')
            return row['result']
    try:
        async with asyncio.timeout(timeout):
            await call(1, 'initialize', {'clientInfo': {'name':'autocanvas','version':'2.0'}, 'capabilities':{'experimentalApi':True}})
            await send({'method':'initialized'})
            models=[];cursor=None;seen=set();number=2
            while True:
                params={'limit':100,'includeHidden':False}
                if cursor:params['cursor']=cursor
                page=await call(number,'model/list',params);number+=1
                for row in page.get('data',[]):
                    name=row.get('model') or row.get('id')
                    if name and not row.get('hidden') and name not in models:models.append(name)
                cursor=page.get('nextCursor')
                if not cursor:break
                if cursor in seen:raise ValueError('Codex 返回重复的模型分页游标')
                seen.add(cursor)
            if not models:raise ValueError('Codex 未返回可选模型；可以手动输入模型名称')
            return models
    except asyncio.TimeoutError:
        raise ValueError('模型查询超时，请检查 Codex 网络和登录状态后重试') from None
    finally:
        if proc.returncode is None:
            try:proc.terminate()
            except ProcessLookupError:pass
        try:await asyncio.wait_for(proc.wait(),2)
        except asyncio.TimeoutError:
            proc.kill();await proc.wait()
