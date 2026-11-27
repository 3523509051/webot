import importlib.util
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from wechat_pairing import select_groups,build_configuration


class CombinationTests(unittest.TestCase):
    def setUp(self):
        self.groups=[{'id':'a@chatroom','name':'Test'},{'id':'b@chatroom','name':'Other'}]

    def test_initial_selection_is_explicit(self):
        self.assertEqual(select_groups(self.groups,{},choose=lambda _: '1'),self.groups[:1])
        for answer in ('','0','3','all','1,not-a-number'):
            with self.subTest(answer=answer),self.assertRaises(ValueError):
                select_groups(self.groups,{},choose=lambda _:answer)

    def test_all_requires_literal_confirmation(self):
        self.assertEqual(select_groups(self.groups,{},choose=lambda _:'ALL'),self.groups)

    def test_restart_does_not_enable_new_groups(self):
        self.assertEqual(select_groups(self.groups,{'group_ids':{'1':'a@chatroom'}},
                                      choose=lambda _:self.fail('must not prompt')),self.groups[:1])

    def test_missing_selected_groups_does_not_fall_back_to_all(self):
        with self.assertRaises(ValueError):
            select_groups(self.groups,{'group_ids':{'1':'missing@chatroom'}})

    def test_pairing_rejects_account_switch(self):
        with self.assertRaises(ValueError):
            build_configuration({}, {}, {'account_wxid':'new'}, {'account_wxid':'old'},'default')

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
