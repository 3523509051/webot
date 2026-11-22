import tempfile
import unittest
from pathlib import Path


class IdentityTests(unittest.TestCase):

    def test_bridge_member_list_does_not_present_former_members_as_current(self):
        from bridge.identity_directory import IdentityDirectory
        with tempfile.TemporaryDirectory() as tmp:
            d=IdentityDirectory(Path(tmp)/'x.db','bot')
            d.update_members('g',[{'username':'a','nick_name':'甲'},{'username':'b','nick_name':'乙'}])
            d.update_members('g',[{'username':'a','nick_name':'甲的新名字'}])
            self.assertEqual([m['wxid'] for m in d.directory('g')['members']],['a'])

    def test_old_message_binding_needs_unique_raw_history_evidence(self):
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            s=RecordStore(Path(tmp)/'x.db');r=IdentityResolver(s)
            s.append_message('g',{'user':'旧名字','text':'我的电脑打不开了','ts':105})
            s.append_message('g',{'user':'同名','text':'好','ts':106})
            observations=[{'pid':'a','text':'我的电脑打不开了','ts':100},{'pid':'a','text':'好','ts':100},{'pid':'b','text':'好','ts':101}]
            self.assertEqual(r.bind_message_sources('g',observations),1)
            self.assertEqual(s.search_messages('g',['电脑'])[0]['person_id'],'a')
            self.assertIsNone(s.search_messages('g',['好'])[0]['person_id'])

    def test_verified_alias_and_duplicate_name_do_not_merge_people(self):
        from plugins.astrbot_plugin_group_activation.identity import IdentityResolver
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            resolver=IdentityResolver(RecordStore(Path(tmp)/'x.db'))
            resolver.ingest('g',{'account':'bot','group_wxid':'g@chatroom','members':[
                {'wxid':'wxid_one','user_id':'1','nickname':'陈sir','aliases':[{'name':'Amy','source':'reference'}]},
                {'wxid':'wxid_two','user_id':'2','nickname':'AMYYYYYY','aliases':[]}]})
            one=resolver.resolve('g','Amy')
            self.assertEqual(one['status'],'found')
            self.assertEqual(one['people'][0]['wxid'],'wxid_one')
            self.assertEqual(resolver.resolve('g','AMYYYYYY')['people'][0]['wxid'],'wxid_two')
            resolver.ingest('g',{'account':'bot','members':[{'wxid':'wxid_two','user_id':'2','nickname':'Amy'}]})
            self.assertEqual(resolver.resolve('g','Amy')['status'],'ambiguous')
            self.assertEqual(resolver.resolve('other','Amy')['status'],'not_found')

    def test_bridge_uses_wxid_not_name_and_preserves_reference_identity(self):
        from bridge.identity_directory import IdentityDirectory, bridge_id
        with tempfile.TemporaryDirectory() as tmp:
            d=IdentityDirectory(Path(tmp)/'x.db','bot')
            d.update_members('group@chatroom',[{'username':'wxid_one','nick_name':'陈sir'}])
            d.observe_reference('group@chatroom','<msg><appmsg><refermsg><chatusr>wxid_one</chatusr><displayname>Amy</displayname></refermsg></appmsg></msg>')
            member=d.directory('group@chatroom')['members'][0]
            self.assertEqual(member['user_id'],bridge_id('wxid_one'))
            self.assertIn('Amy',[x['name'] for x in member['aliases']])
            d.register_message('session1',1,'group@chatroom',{'sender_wxid':'wxid_one','local_id':7,'seq':200,'sender_name':'陈sir'})
            self.assertIsNone(d.message('session2',1))
            self.assertEqual(d.message('session1',1)['sender_wxid'],'wxid_one')


if __name__=='__main__':unittest.main()
