import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


class IndexJobTests(unittest.IsolatedAsyncioTestCase):


    async def test_purge_during_upload_removes_late_index(self):
        import asyncio
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.index_jobs import IndexJobs
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db');s.save_memory('g','应该被删除')
            entered=asyncio.Event();release=asyncio.Event()
            async def upload(**kwargs):
                entered.set();await release.wait();return types.SimpleNamespace(doc_id='late-doc')
            helper=types.SimpleNamespace(list_documents=AsyncMock(return_value=[]),upload_document=upload,delete_document=AsyncMock())
            jobs=IndexJobs(s,types.SimpleNamespace(_helper=AsyncMock(return_value=helper)))
            task=asyncio.create_task(jobs.run_once());await entered.wait()
            s.purge_group('g');release.set();await task
            helper.delete_document.assert_awaited_once_with('late-doc')
            self.assertEqual(s.search_memories('g'),[])

    async def test_retry_after_upload_commit_does_not_duplicate(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.index_jobs import IndexJobs
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            saved=s.save_memory('g','x'*500)
            helper=types.SimpleNamespace(list_documents=AsyncMock(return_value=[]),upload_document=AsyncMock(return_value=types.SimpleNamespace(doc_id='doc1')))
            kbs=types.SimpleNamespace(_helper=AsyncMock(return_value=helper))
            jobs=IndexJobs(s,kbs)
            await jobs.run_once()
            self.assertIn('x'*500,helper.upload_document.call_args.kwargs['pre_chunked_text'][0])
            with s.transaction() as c:c.execute("UPDATE index_jobs SET state='pending',next_ts=0")
            helper.list_documents.return_value=[types.SimpleNamespace(doc_id='doc1',doc_name='webot-'+saved['id']+'.txt')]
            await jobs.run_once()
            self.assertEqual(helper.upload_document.await_count,1)


if __name__=='__main__':unittest.main()
