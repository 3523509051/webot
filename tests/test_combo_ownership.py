import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from wechat_pairing import build_configuration


class CombinationTests(unittest.TestCase):
    def setUp(self):
        self.groups=[{'id':'a@chatroom','name':'Test'},{'id':'b@chatroom','name':'Other'}]

    def test_all_scanned_groups_enabled_and_old_selection_does_not_filter_new_group(self):
        core={'agents':[{'agent_id':'default'}],'channels':{'onebot11':{'port':6200}}}
        previous={'account_wxid':'bot','group_ids':{'1':'a@chatroom'}}
        result,bridge,labels,state=build_configuration(core,{'astrbot':{'self_id':10001}},
            {'account_wxid':'bot','groups':self.groups},previous,'default')
        self.assertEqual(set(bridge['wechat']['groups']),{'a@chatroom','b@chatroom'})
        self.assertEqual(len(result['orchestration']['allowed_conversations']),2)
        self.assertEqual(len(result['orchestration']['send_targets']),2)

    def test_group_missing_from_scan_is_not_monitored_by_bridge(self):
        core={'agents':[{'agent_id':'default'}]}
        _,bridge,_,_=build_configuration(core,{'astrbot':{'self_id':10001}},
            {'account_wxid':'bot','groups':self.groups[:1]},
            {'account_wxid':'bot','group_ids':{'1':'b@chatroom'}},'default')
        self.assertEqual(set(bridge['wechat']['groups']),{'a@chatroom'})

    def test_pairing_rejects_account_switch(self):
        with self.assertRaises(ValueError):
            build_configuration({}, {}, {'account_wxid':'new'}, {'account_wxid':'old'},'default')

    def test_no_selection_script_or_prompt_remains(self):
        self.assertFalse((ROOT/'setup.cmd').exists())
        source=(ROOT/'scripts/wechat_pairing.py').read_text(encoding='utf-8')
        self.assertNotIn('select_groups',source)

    def test_supervisor_is_owned_by_webot_and_uses_separate_bridge_python(self):
        import combo_launcher
        self.assertEqual(combo_launcher.ROOT,ROOT)
        self.assertEqual(combo_launcher.PERO_ROOT,ROOT.parent/'Pero')
        source=(ROOT/'scripts/combo_launcher.py').read_text(encoding='utf-8')
        self.assertIn("InstanceLock(ROOT/'data/launcher.lock')",source)
        self.assertIn("PERO_ROOT/'src'",source)
        self.assertIn("str(pairing['python'])",source)
        self.assertNotIn("parser.add_argument('--with-webot'",source)


if __name__=='__main__':unittest.main()
