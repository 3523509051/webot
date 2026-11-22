import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta


class MemoryServiceTests(unittest.TestCase):
    def test_unknown_self_does_not_receive_someone_elses_explicit_notes(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.retrieval import MemoryRetrieval
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db');s.save_memory('g','别人的事项',kind='explicit',person_id='other',explicit=True)
            r=MemoryRetrieval(s,IdentityResolver(s)).lookup('g','我之前让你记住什么',person_id=None)
            self.assertIn(r['status'],('ambiguous','unavailable'))
            self.assertNotIn('别人的事项',r['text'])

    def test_single_character_member_can_be_resolved_in_a_direct_profile_question(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.retrieval import MemoryRetrieval
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db');identity=IdentityResolver(s)
            identity.ingest('g',{'account':'bot','members':[{'wxid':'wxid_a','user_id':'1','nickname':'昴'}]})
            pid=identity.resolve('g','昴')['people'][0]['pid']
            s.save_memory('g','喜欢天文',kind='profile_fact',person_id=pid)
            r=MemoryRetrieval(s,identity).lookup('g','昴是谁')
            self.assertEqual(r['status'],'found')
            self.assertEqual(r['person']['pid'],pid)

    def test_person_date_query_keeps_topic_instead_of_only_latest_chatter(self):
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.retrieval import MemoryRetrieval
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db')
            now=datetime(2026,11,14,10,tzinfo=timezone(timedelta(hours=8))).timestamp()
            s.append_message('g',{'user':'甲','person_id':'p','text':'我的电脑打不开了','ts':now-86400})
            for i in range(20):s.append_message('g',{'user':'甲','person_id':'p','text':'讨论其他事情'+str(i),'ts':now-86400+i+1})
            r=MemoryRetrieval(s,IdentityResolver(s)).lookup('g','我昨天提到电脑打不开是什么情况',person_id='p',now=now)
            self.assertIn('我的电脑打不开了',r['text'])

    def test_historical_save_query_reports_superseded_and_cancelled_states(self):
        from plugins.astrbot_plugin_group_activation.memory_service import MemoryService
        from plugins.astrbot_plugin_group_activation.retrieval import MemoryRetrieval
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db');service=MemoryService(s)
            now=datetime(2026,11,14,10,tzinfo=timezone(timedelta(hours=8))).timestamp()
            old=service.capture('g','p','记住：周三交初稿',now=now-86400)[0]
            new=service.capture('g','p','把刚才记住的改成周四交初稿',now=now)[0]
            query=MemoryRetrieval(s,IdentityResolver(s)).lookup('g','我昨天让你记住什么',person_id='p',now=now)
            self.assertIn('superseded',query['text'])
            self.assertIn('周四',query['text'])
            service.capture('g','p','取消刚才记住的周四交初稿')
            query=MemoryRetrieval(s,IdentityResolver(s)).lookup('g','我之前让你记住什么',person_id='p',now=now)
            self.assertIn('cancelled',query['text'])

    def test_explicit_negation_quotes_multiple_items_and_corrections(self):
        from plugins.astrbot_plugin_group_activation.memory_service import MemoryService,save_intent
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=MemoryService(RecordStore(Path(tmp)/'x.db'))
            for text in ('不要记住这件事','他说“记住周三交初稿”','我昨天让你记住什么','你能记住吗'):
                self.assertIsNone(save_intent(text))
            self.assertEqual(save_intent('我想让你记住：周三交初稿')['content'],'周三交初稿')
            r=s.capture('g','p','请记住：周三交初稿；另外，周五检查格式')
            self.assertEqual(len(r),2)
            self.assertTrue(all(x['status']=='saved_index_pending' for x in r))
            r2=s.capture('g','p','记住：周三交初稿')
            self.assertEqual(r2[0]['status'],'already_exists')
            r3=s.capture('g','p','把刚才记住的改成周四交初稿')
            self.assertEqual(r3[0]['status'],'saved_index_pending')
            self.assertIn('周四',r3[0]['text'])
            self.assertEqual(s.store.get_memory(r[0]['id'])['state'],'superseded')

    def test_dates_and_local_failure_are_not_success(self):
        from plugins.astrbot_plugin_group_activation.memory_service import MemoryService,resolve_event_time
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            service=MemoryService(RecordStore(Path(tmp)/'x.db'))
            ts=datetime(2026,11,14,10,tzinfo=timezone(timedelta(hours=8))).timestamp()
            self.assertEqual(resolve_event_time('下周三交初稿',ts),'2026-11-18')
            with patch.object(service.store,'save_memory',side_effect=OSError('disk full')):
                result=service.capture('g','p','记住：周三交初稿')
                self.assertEqual(result[0]['status'],'failed')
                self.assertNotIn('已记住',service.receipt(result))

    def test_retrieval_by_date_and_identity_without_subject_keywords(self):
        from plugins.astrbot_plugin_group_activation.retrieval import MemoryRetrieval
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            store=RecordStore(Path(tmp)/'x.db');identity=IdentityResolver(store)
            identity.ingest('g',{'account':'bot','members':[{'wxid':'wxid_a','user_id':'1','nickname':'陈sir','aliases':[{'name':'Amy','source':'reference'}]}]})
            pid=identity.resolve('g','Amy')['people'][0]['pid']
            now=datetime(2026,11,14,10,tzinfo=timezone(timedelta(hours=8))).timestamp()
            store.save_memory('g','周三交初稿',kind='explicit',person_id=pid,explicit=True,recorded_at=now-86400)
            r=MemoryRetrieval(store,identity).lookup('g','Amy昨天让你记住什么',now=now)
            self.assertEqual(r['status'],'found')
            self.assertIn('周三交初稿',r['text'])
            self.assertEqual(MemoryRetrieval(store,identity).lookup('other','昨天让我记住什么',person_id=pid,now=now)['status'],'not_found')

    def test_cancel_and_profile_subject_are_distinct_from_author(self):
        from plugins.astrbot_plugin_group_activation.memory_service import MemoryService
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            store=RecordStore(Path(tmp)/'x.db');identity=IdentityResolver(store)
            identity.ingest('g',{'account':'bot','members':[
                {'wxid':'wxid_a','user_id':'1','nickname':'甲'},
                {'wxid':'wxid_b','user_id':'2','nickname':'Amy'}]})
            a=identity.resolve('g','甲')['people'][0]['pid'];b=identity.resolve('g','Amy')['people'][0]['pid']
            service=MemoryService(store,identity)
            saved=service.capture('g',a,'记住：Amy不吃香菜')[0]
            self.assertEqual(len(store.search_memories('g',person_id=a,explicit=True)),1)
            self.assertEqual(len(store.search_memories('g',person_id=a,kind='profile_fact')),0)
            self.assertEqual(len(store.search_memories('g',person_id=b,kind='profile_fact')),1)
            cancelled=service.capture('g',a,'取消刚才记住的Amy不吃香菜')[0]
            self.assertEqual(cancelled['status'],'cancelled')
            self.assertEqual(store.get_memory(saved['id'])['state'],'cancelled')


if __name__=='__main__':unittest.main()
