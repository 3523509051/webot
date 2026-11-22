import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta


class JournalTests(unittest.IsolatedAsyncioTestCase):

    async def test_midnight_is_split_and_unsummarized_days_have_an_index(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.journal import Journal
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            now=datetime(2026,11,14,0,1,tzinfo=timezone(timedelta(hours=8))).timestamp()
            for i,ts in enumerate((now-120,now)):s.append_message('g',{'user':'甲','text':f'事项{i}','ts':ts})
            async def summarize(text):return '[话题] 交接\n[结论] 已记录'
            journal=Journal(s,summarize)
            journal.rebuild_daily('g')
            with s.transaction() as c:self.assertEqual(c.execute('select count(*) from daily_indexes').fetchone()[0],2)
            await journal.archive('g',force=True)
            self.assertEqual(len(s.summaries('g')),2)

    async def test_chunks_preserve_tail_and_retry_after_failure(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.journal import Journal
        with tempfile.TemporaryDirectory() as tmp:
            store=RecordStore(Path(tmp)/'x.db')
            for i in range(110):store.append_message('g',{'user':'甲','text':str(i)+'x'*100,'ts':i})
            calls=[]
            async def summarize(text):
                calls.append(text)
                if len(calls)==1:raise TimeoutError('offline')
                return '[话题] 初稿\n[要点] '+text[-100:]
            journal=Journal(store,summarize,max_chars=1500)
            await journal.archive('g',force=True)
            self.assertEqual(len(store.archive_candidates('g',200)),110)
            await journal.archive('g',force=True)
            self.assertEqual(len(store.archive_candidates('g',200)),0)
            self.assertTrue(any('109xxx' in t for t in calls))
            self.assertEqual(store.message_count('g'),110)


if __name__=='__main__':unittest.main()
