"""Identity checks must fail before a different desktop/account can be used."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from account_binding import (AccountBinding, BindingError, BindingGuard,
                             load_binding, save_binding, verify_database,
                             account_from_files, binding_code_seen, BoundDatabase)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'wxid_bot_ab' / 'db_storage').mkdir(parents=True)
        self.binding = AccountBinding(str(self.root), 'wxid_bot_ab', 'wxid_bot')
        self.db = SimpleNamespace(account_dir=str(self.root / 'wxid_bot_ab'),
                                  get_self_info=lambda: {'username': 'wxid_bot'})

    def test_roundtrip_and_missing_binding_fails(self):
        path = self.root / 'binding.json'
        with self.assertRaises(BindingError):
            load_binding(path)
        save_binding(path, self.binding)
        self.assertEqual(load_binding(path), self.binding)
        self.assertNotIn('pid', json.loads(path.read_text(encoding='utf-8')))

    def test_path_escape_rejected(self):
        for account in ('../other', '..', 'a/b', 'a\\b', 'C:other', ''):
            with self.subTest(account=account), self.assertRaises(BindingError):
                AccountBinding(str(self.root), account, 'wxid_bot').validate()

    def test_database_fallback_or_wrong_identity_rejected(self):
        verify_database(self.binding, self.db)
        self.db.account_dir = str(self.root / 'other')
        with self.assertRaises(BindingError):
            verify_database(self.binding, self.db)
        self.db.account_dir = str(self.root / 'wxid_bot_ab')
        self.db.get_self_info = lambda: {'username': 'wxid_other'}
        with self.assertRaises(BindingError):
            verify_database(self.binding, self.db)

    def test_open_files_proof_rejects_missing_or_ambiguous_accounts(self):
        one = str(self.root / 'wxid_bot_ab' / 'db_storage' / 'session.db')
        other = str(self.root / 'wxid_other_cd' / 'db_storage' / 'contact.db')
        self.assertEqual(account_from_files(self.root, [one]), 'wxid_bot_ab')
        for paths in ([], [one, other], [str(self.root / 'wxid_bot_ab' / 'unrelated.txt')]):
            with self.assertRaises(BindingError):
                account_from_files(self.root, paths)

    def test_guard_latches_process_reuse_account_switch_and_foreign_window(self):
        for changed in ('created', 'account', 'session', 'hwnd', 'pid'):
            proof = dict(pid=10, created=100, session=1, hwnd=30, account='wxid_bot_ab')
            guard = BindingGuard(self.binding, proof.copy(), lambda: proof.copy())
            guard.check()
            proof[changed] = 'different'
            with self.assertRaises(BindingError):
                guard.check()
            proof.update(pid=10, created=100, session=1, hwnd=30, account='wxid_bot_ab')
            with self.assertRaises(BindingError):
                guard.check()  # Must explicitly bind again after identity loss.

    def test_binding_code_requires_fresh_self_message(self):
        row = {'content': 'WEBOT-BIND-test', 'sender_username': 'wxid_bot', 'seq': 12}
        self.assertTrue(binding_code_seen([row], 'WEBOT-BIND-test', 'wxid_bot', 11))
        self.assertFalse(binding_code_seen([row], 'WEBOT-BIND-test', 'wxid_bot', 12))
        row['sender_username'] = 'other'
        self.assertFalse(binding_code_seen([row], 'WEBOT-BIND-test', 'wxid_bot', 11))

    def test_database_cannot_return_data_after_internal_account_fallback(self):
        proof = dict(pid=10, created=100, session=1, hwnd=30, account='wxid_bot_ab')
        guard = BindingGuard(self.binding, proof, lambda: proof.copy())
        def query():
            self.db.account_dir = str(self.root / 'other')
            return ['wrong-account-data']
        self.db.get_groups = query
        bound = BoundDatabase(self.db, self.binding, guard)
        with self.assertRaises(BindingError):
            bound.get_groups()
        self.db.account_dir = str(self.root / 'wxid_bot_ab')
        with self.assertRaises(BindingError):
            guard.check()


if __name__ == '__main__':
    unittest.main()
