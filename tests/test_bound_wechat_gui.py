import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from account_binding import AccountBinding, BindingGuard, BindingError
from bound_wechat_gui import ScopedControl, GuardedInput, driver_classes


class ScopedDriverTests(unittest.TestCase):
    def setUp(self):
        self.proof = dict(pid=10, created=20, hwnd=30, session=1, account='bot')
        self.guard = BindingGuard(AccountBinding('/unused', 'bot', 'wxid_bot'), self.proof,
                                  lambda: self.proof.copy())
        self.desktop = SimpleNamespace(require_foreground=Mock(), require_point=Mock(),
                                       require_window=Mock(), process_windows=lambda guard: [30])

    def test_foreign_control_cannot_receive_sendkeys(self):
        raw = SimpleNamespace(ProcessId=999, SendKeys=Mock())
        control = ScopedControl(raw, self.desktop, self.guard)
        with self.assertRaises(BindingError):
            control.SendKeys('{Enter}')
        raw.SendKeys.assert_not_called()

    def test_foreground_mismatch_blocks_enter_even_for_correct_control(self):
        raw = SimpleNamespace(ProcessId=10, SendKeys=Mock())
        control = ScopedControl(raw, self.desktop, self.guard)
        self.desktop.require_foreground.side_effect = BindingError('wrong foreground')
        with self.assertRaises(BindingError):
            control.SendKeys('{Enter}')
        raw.SendKeys.assert_not_called()

    def test_children_from_another_process_are_not_search_candidates(self):
        own = SimpleNamespace(ProcessId=10)
        other = SimpleNamespace(ProcessId=999)
        raw = SimpleNamespace(ProcessId=10, GetChildren=lambda: [own, other])
        children = ScopedControl(raw, self.desktop, self.guard).GetChildren()
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].ProcessId, 10)

    def test_input_guard_stops_keyboard_and_coordinates(self):
        raw = SimpleNamespace(key=Mock(), real_click=Mock())
        guarded = GuardedInput(raw, self.desktop, self.guard)
        self.desktop.require_foreground.side_effect = BindingError('wrong foreground')
        with self.assertRaises(BindingError):
            guarded.key(13)
        raw.key.assert_not_called()
        self.desktop.require_point.side_effect = BindingError('wrong window')
        with self.assertRaises(BindingError):
            guarded.real_click(1, 2)
        raw.real_click.assert_not_called()

    def test_uia_rediscovery_and_search_are_scoped(self):
        root = SimpleNamespace(ProcessId=10, ControlTypeName='ListControl', AutomationId='search')
        class UIA:
            def __init__(self): pass
            def _anchor(self, hwnd): return root
        gui_module = SimpleNamespace(WeChatGUI=object)
        uia_module = SimpleNamespace(WeChatUIA=UIA, SEARCH_LIST_AIDS=('search',),
                                     _aid_hit=lambda value, ids: value in ids,
                                     _find_by=lambda node, pred, **kw: node if pred(node) else None)
        _, cls = driver_classes(gui_module, uia_module)
        uia = cls(self.desktop, self.guard)
        self.assertEqual(uia._wechat_hwnds(), [30])
        self.assertEqual(uia._find_search_list(timeout=0).ProcessId, 10)
        self.desktop.require_window.assert_called_with(self.guard, 30)
        self.assertEqual(uia._resolve_search_keyword('name'), ['name'])
        with self.assertRaises(BindingError):
            uia.wake()
        self.proof['account'] = 'other'
        with self.assertRaises(BindingError):
            uia._wechat_hwnds()


if __name__ == '__main__':
    unittest.main()
