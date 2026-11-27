"""Keep the third-party GUI and UIA paths inside one verified WeChat process.

Adapters live in webot; never patch the installed driver or Pero at runtime.
"""
import time
from account_binding import BindingError


class ScopedControl:
    def __init__(self, control, desktop, guard):
        self._control, self._desktop, self._guard = control, desktop, guard

    def _check(self):
        proof = self._guard.check_runtime()
        if int(self._control.ProcessId) != proof['pid']:
            self._guard.failure = 'UIA 控件不属于绑定的微信，已暂停。'
            raise BindingError(self._guard.failure)

    def __getattr__(self, name):
        self._check()
        value = getattr(self._control, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            self._check()
            if name in ('SendKeys', 'Click', 'RightClick', 'WheelDown', 'WheelUp'):
                self._guard.check()
                self._desktop.require_foreground(self._guard)
            result = value(*args, **kwargs)
            self._check()
            if name == 'GetChildren':
                return [ScopedControl(c, self._desktop, self._guard) for c in result
                        if int(c.ProcessId) == self._guard.proof['pid']]
            if name in ('GetParentControl', 'GetFirstChildControl', 'GetNextSiblingControl') and result:
                return ScopedControl(result, self._desktop, self._guard)
            return result
        return call


class GuardedInput:
    def __init__(self, raw, desktop, guard):
        self._raw, self._desktop, self._guard = raw, desktop, guard

    def __getattr__(self, name):
        value = getattr(self._raw, name)
        if not callable(value):
            return value
        def call(*args, **kwargs):
            self._guard.check()
            if name in ('key', '_raw_key', 'type_unicode', 'type_pinyin'):
                self._desktop.require_foreground(self._guard)
            elif name in ('real_click', 'send_input_click'):
                self._desktop.require_point(self._guard, args[0], args[1])
            elif name == 'wheel':
                self._desktop.require_point(self._guard)
            return value(*args, **kwargs)
        return call


def driver_classes(gui_module, uia_module):
    """Construct subclasses without importing Windows dependencies in tests."""
    class BoundUIA(uia_module.WeChatUIA):
        def __init__(self, desktop, guard):
            self._bound_desktop, self._bound_guard = desktop, guard
            super().__init__()

        def _wechat_hwnds(self, diag=None):
            proof = self._bound_guard.check_runtime()
            if diag is not None:
                diag.update(win32=True, matched=1, kept=1, dropped_no_dll=0)
            return [proof['hwnd']]

        def _anchor(self, hwnd):
            self._bound_desktop.require_window(self._bound_guard, hwnd)
            control = super()._anchor(hwnd)
            return ScopedControl(control, self._bound_desktop, self._bound_guard) if control else None

        def _find_search_list(self, timeout=3.0):
            # The upstream implementation searches the whole desktop. Restrict
            # both older and newer AutomationId forms to this process's trees.
            deadline = time.monotonic() + timeout
            while True:
                for hwnd in self._bound_desktop.process_windows(self._bound_guard):
                    root = self._anchor(hwnd)
                    hit = uia_module._find_by(
                        root, lambda c: c.ControlTypeName == 'ListControl'
                        and uia_module._aid_hit(getattr(c, 'AutomationId', ''), uia_module.SEARCH_LIST_AIDS),
                        max_depth=25)
                    if hit is not None:
                        return hit
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.1)

        @staticmethod
        def _resolve_search_keyword(keyword):
            # The upstream helper creates an unbound WeChatDB of its own.
            return [keyword]

        def wake(self):
            raise BindingError('绑定模式不自动启动微信，请手动登录后重新检查绑定。')

        def _auto_login(self):
            return False

        def _force_foreground(self, hwnd):
            self._bound_desktop.require_window(self._bound_guard, hwnd)
            return super()._force_foreground(hwnd)

        def _clear_transparent(self, x, y):
            # Do not change styles of another account's window to reach ours.
            return []

        def _set_cursor(self, x, y):
            self._bound_desktop.require_point(self._bound_guard, x, y)
            return super()._set_cursor(x, y)

        def _left_click(self):
            self._bound_guard.check()
            self._bound_desktop.require_point(self._bound_guard)
            return super()._left_click()

        def _right_click(self):
            self._bound_guard.check()
            self._bound_desktop.require_point(self._bound_guard)
            return super()._right_click()

        def _mouse_wheel(self, delta):
            self._bound_guard.check()
            self._bound_desktop.require_point(self._bound_guard)
            return super()._mouse_wheel(delta)

    class BoundGUI(gui_module.WeChatGUI):
        def __init__(self, desktop, guard):
            self._bound_desktop, self._bound_guard = desktop, guard
            proof = guard.check()
            super().__init__(hwnd=proof['hwnd'])
            self._input = GuardedInput(self._input, desktop, guard)

        def _get_uia(self, refresh=False):
            self._bound_guard.check()
            if refresh or self._uia is None:
                self._uia = BoundUIA(self._bound_desktop, self._bound_guard)
            # Upstream self-healing remains available, but all rediscovery goes
            # through BoundUIA._wechat_hwnds rather than a desktop-wide search.
            if self._uia.ensure_window(wake=False):
                return self._uia
            if self._uia.ensure_materialized(timeout=2.0) and self._uia.ensure_window(wake=False):
                return self._uia
            return None

        def use_window(self, hwnd):
            self._bound_desktop.require_window(self._bound_guard, hwnd)
            return super().use_window(hwnd)

        def bring_to_front(self, *args, **kwargs):
            self._bound_desktop.require_window(self._bound_guard, self.main_hwnd)
            return super().bring_to_front(*args, **kwargs)

        def point_belongs_to_wechat(self, x, y):
            self._bound_desktop.require_point(self._bound_guard, x, y)
            return True

        def _minimize_blockers(self):
            return 0

    return BoundGUI, BoundUIA


def create_gui(desktop, guard):
    from wechatauto import guia, uia_driver
    gui_class, _ = driver_classes(guia, uia_driver)
    return gui_class(desktop, guard)
