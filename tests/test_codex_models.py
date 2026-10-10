import json
import tempfile
import unittest
from pathlib import Path
from homework_service.models import list_models


class ModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_handshake_pagination_and_hidden_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);exe=root/'codex'
            exe.write_text('''#!/usr/bin/env python3
import sys,json
ready=False
for line in sys.stdin:
 r=json.loads(line)
 if r['method']=='initialize':result={}
 elif r['method']=='initialized':ready=True;continue
 elif r['method']=='model/list':
  assert ready
  result={'data':[{'model':'current-model','hidden':False},{'model':'hidden-model','hidden':True}],'nextCursor':'next'} if not r['params'].get('cursor') else {'data':[{'model':'other-model'},{'model':'current-model'}],'nextCursor':None}
 else:raise RuntimeError('Unexpected method')
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
''');exe.chmod(0o755)
            self.assertEqual(await list_models(str(exe),root),['current-model','other-model'])

    async def test_query_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);exe=root/'codex'
            exe.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n');exe.chmod(0o755)
            with self.assertRaisesRegex(ValueError,'超时'):
                await list_models(str(exe),root,timeout=.1)
