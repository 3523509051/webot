import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from account_binding import BindingError
from wechat_client import WeChatClient


class BindingSendBoundaryTests(unittest.TestCase):
    def test_all_public_send_routes_stop_before_gui_when_binding_is_lost(self):
        routes = [
            ('send_text', ('private:wxid_person', 'text')),
            ('send_image', ('group', 'missing.png')),
            ('send_text_and_image', ('group', 'text', 'missing.png')),
            ('send_at', ('group', 'person', '', 'text')),
            ('send_at_multi', ('group', [{'type': 'text', 'text': 'text'}])),
        ]
        for name, args in routes:
            with self.subTest(route=name):
                client = WeChatClient.__new__(WeChatClient)
                client._binding_guard = SimpleNamespace(check=Mock(side_effect=BindingError('lost')))
                client.gui = Mock()
                client._confirmed_target = {'window': 30}
                result = getattr(client, name)(*args)
                self.assertEqual(result.state, 'failed')
                self.assertIsNone(client._confirmed_target)
                self.assertEqual(client.gui.mock_calls, [])

    def test_wrong_database_identity_is_rejected_before_gui_construction(self):
        import tempfile
        from account_binding import AccountBinding
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bot' / 'db_storage').mkdir(parents=True)
            binding = AccountBinding(str(root), 'bot', 'wxid_bot')
            db = SimpleNamespace(account_dir=str(root / 'different'), get_self_info=lambda: {'username': 'other'})
            fake_module = SimpleNamespace(WeChatDB=Mock(return_value=db))
            with patch.dict(sys.modules, {'wechatauto': fake_module}), \
                 patch('account_binding.load_binding', return_value=binding), \
                 patch('wechat_desktop.Desktop', return_value=SimpleNamespace(guard=Mock())), \
                 patch('bound_wechat_gui.create_gui') as create_gui:
                with self.assertRaises(BindingError):
                    WeChatClient(db_dir=str(root))
                create_gui.assert_not_called()
                fake_module.WeChatDB.assert_called_once_with(db_dir=str(root), account='bot')


if __name__ == '__main__':
    unittest.main()
