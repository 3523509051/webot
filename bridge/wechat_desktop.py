"""Read-only Win32 inventory for the current interactive desktop."""
import os
from pathlib import Path
from account_binding import BindingError, account_from_files, BindingGuard


class Desktop:
    def __init__(self):
        import psutil
        import win32gui
        import win32process
        import win32ts
        self.psutil, self.gui, self.process, self.ts = psutil, win32gui, win32process, win32ts
        self.session = win32ts.ProcessIdToSessionId(os.getpid())

    def pid(self, hwnd):
        if not self.gui.IsWindow(hwnd):
            raise BindingError('绑定的微信窗口已经关闭。')
        return self.process.GetWindowThreadProcessId(hwnd)[1]

    def windows(self):
        result = []
        def visit(hwnd, _):
            try:
                pid = self.pid(hwnd)
                if (self.gui.IsWindowVisible(hwnd)
                        and self.psutil.Process(pid).name().lower() in ('weixin.exe', 'wechat.exe')
                        and self.gui.GetWindowText(hwnd).strip() in ('微信', 'Weixin', 'WeChat')
                        and self.ts.ProcessIdToSessionId(pid) == self.session):
                    result.append(hwnd)
            except Exception:
                pass
            return True
        self.gui.EnumWindows(visit, None)
        return result

    def runtime_proof(self, hwnd):
        pid = self.pid(hwnd)
        process = self.psutil.Process(pid)
        if process.name().lower() not in ('weixin.exe', 'wechat.exe'):
            raise BindingError('窗口不属于微信进程。')
        session = self.ts.ProcessIdToSessionId(pid)
        if session != self.session:
            raise BindingError('请在微信登录用户的交互桌面运行，不要从 SSH 启动。')
        return dict(pid=pid, created=process.create_time(), session=session, hwnd=hwnd)

    def proof(self, hwnd, root):
        result = self.runtime_proof(hwnd)
        process = self.psutil.Process(result['pid'])
        result['account'] = account_from_files(root, [file.path for file in process.open_files()])
        if self.runtime_proof(hwnd) != {k: v for k, v in result.items() if k != 'account'}:
            raise BindingError('核对数据库期间微信进程发生变化。')
        return result

    def guard(self, binding):
        candidates = []
        for hwnd in self.windows():
            try:
                proof = self.proof(hwnd, binding.db_dir)
                if proof['account'] == binding.account:
                    candidates.append(proof)
            except Exception:
                continue
        if len(candidates) != 1:
            raise BindingError('未找到唯一的已绑定微信窗口；请回到单号登录流程检查绑定。')
        proof = candidates[0]
        return BindingGuard(binding, proof, lambda: self.proof(proof['hwnd'], binding.db_dir),
                            lambda: self.runtime_proof(proof['hwnd']))

    def require_window(self, guard, hwnd):
        proof = guard.check_runtime()
        if self.pid(hwnd) != proof['pid']:
            guard.failure = '操作目标不是绑定的微信实例，已暂停。'
            raise BindingError(guard.failure)

    def require_foreground(self, guard):
        self.require_window(guard, self.gui.GetForegroundWindow())

    def require_point(self, guard, x=None, y=None):
        if x is None:
            x, y = self.gui.GetCursorPos()
        self.require_window(guard, self.gui.WindowFromPoint((int(x), int(y))))

    def process_windows(self, guard):
        proof = guard.check_runtime()
        result = []
        def visit(hwnd, _):
            if self.gui.IsWindowVisible(hwnd) and self.pid(hwnd) == proof['pid']:
                result.append(hwnd)
            return True
        self.gui.EnumWindows(visit, None)
        return result
