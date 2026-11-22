"""Durable source storage must outlive the L2 cache and process restarts."""
import tempfile
import unittest
from pathlib import Path


class RecordStoreTests(unittest.TestCase):
    def test_forgetting_profiles_does_not_reactivate_a_previously_cleared_index_job(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            s.save_memory('g','明确原文仍可本地查询',kind='explicit',explicit=True)
            s.forget('g','knowledge')
            self.assertEqual(s.pending_jobs(),[])
            s.forget('g','profile')
            self.assertEqual(s.pending_jobs(),[])

    def test_full_history_survives_cache_limit_restart_and_duplicate_delivery(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'memory.sqlite3'
            store=RecordStore(p)
            for i in range(1200):
                store.append_message('g',{'user':'甲','text':str(i),'ts':i,'source_key':f'g:{i}'})
            store.append_message('g',{'user':'甲','text':'1199','ts':1199,'source_key':'g:1199'})
            restored=RecordStore(p)
            self.assertEqual(restored.message_count('g'),1200)
            self.assertEqual(len(restored.recent_messages('g',500)),500)
            self.assertEqual(restored.search_messages('g',['0'],limit=2000)[0]['text'],'0')

    def test_context_reset_does_not_destroy_sources_and_other_groups(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            s.append_message('a',{'user':'甲','text':'old','ts':1})
            s.append_message('b',{'user':'乙','text':'other','ts':1})
            s.reset_context('a')
            self.assertEqual(s.recent_messages('a'),[])
            self.assertEqual(s.message_count('a'),1)
            self.assertEqual(len(s.recent_messages('b')),1)
            self.assertEqual(len(s.search_messages('a',['old'])),1)

    def test_memory_versions_jobs_and_purge_do_not_resurrect(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            first=s.save_memory('g','周三交初稿',kind='explicit',person_id='p',explicit=True)
            again=s.save_memory('g','周三交初稿',kind='explicit',person_id='p',explicit=True)
            self.assertEqual(again['status'],'already_exists')
            newer=s.save_memory('g','周四交初稿',kind='explicit',person_id='p',explicit=True,replaces=first['id'])
            self.assertEqual(s.get_memory(first['id'])['state'],'superseded')
            self.assertEqual(s.get_memory(newer['id'])['state'],'active')
            self.assertEqual(len(s.pending_jobs()),1)  # superseded version is no longer indexed
            s.purge_group('g')
            self.assertEqual(s.pending_jobs(),[])
            self.assertEqual(s.search_memories('g'),[])

    def test_archive_sources_are_fixed_and_idempotent(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            for i in range(100):s.append_message('g',{'user':'甲','text':str(i),'ts':i})
            batch=s.archive_candidates('g',limit=80)
            s.append_message('g',{'user':'乙','text':'new','ts':101})
            result=s.commit_summary('g',batch,'话题','实际讨论结论')
            self.assertTrue(result)
            self.assertEqual(len(s.archive_candidates('g',limit=100)),21)
            self.assertEqual(s.message_count('g'),101)
            self.assertEqual(s.commit_summary('g',batch,'话题','实际讨论结论'),result)


if __name__=='__main__':unittest.main()
