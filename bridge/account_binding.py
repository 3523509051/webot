"""Explicit account identity shared by scanning and the running bridge.

No Windows/GUI imports at module load: validation is testable offline.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

DEFAULT_BINDING = Path(__file__).with_name('account-binding.json')


class BindingError(RuntimeError):
    """Identity is missing, ambiguous or has changed; do not fall back."""


@dataclass(frozen=True)
class AccountBinding:
    db_dir: str
    account: str
    wxid: str
    version: int = 1

    def validate(self):
        if (self.version != 1 or not isinstance(self.account, str)
                or not self.account or self.account in ('.', '..')
                or any(c in self.account for c in '/\\:')
                or not isinstance(self.wxid, str) or not self.wxid.strip()
                or self.wxid.endswith('@chatroom')):
            raise BindingError('账号绑定格式无效，请重新绑定。')
        root = Path(self.db_dir)
        if not root.is_absolute() or not root.is_dir():
            raise BindingError('绑定的微信数据根目录不存在。')
        directory = root / self.account
        if directory.resolve().parent != root.resolve() or not (directory / 'db_storage').is_dir():
            raise BindingError('绑定账号目录不存在或越出微信根目录。')
        return self


def load_binding(path=None):
    path = Path(path) if path else DEFAULT_BINDING
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return AccountBinding(**data).validate()
    except (OSError, ValueError, TypeError) as error:
        raise BindingError('缺少有效账号绑定，请在微信桌面运行“绑定微信账号.cmd”。') from error


def save_binding(path, binding):
    binding.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='binding-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(asdict(binding), file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def self_wxid(info):
    if not isinstance(info, dict):
        raise BindingError('无法读取数据库账号身份。')
    return str(info.get('username') or info.get('wxid') or info.get('user_name') or '').strip()


def verify_database(binding, db):
    binding.validate()
    expected = (Path(binding.db_dir) / binding.account).resolve()
    actual = getattr(db, 'account_dir', None)
    if not actual or Path(actual).resolve() != expected:
        raise BindingError('数据库自动选到了其他账号，已停止。')
    if self_wxid(db.get_self_info()) != binding.wxid:
        raise BindingError('数据库内部 wxid 与绑定身份不同，已停止。')
    # get_self_info may itself cause the underlying driver to select another DB.
    if Path(db.account_dir).resolve() != expected:
        raise BindingError('读取身份时数据库发生切换，已停止。')


def account_from_files(root, paths):
    root = Path(root).resolve()
    accounts = set()
    for value in paths:
        try:
            parts = Path(value).resolve().relative_to(root).parts
        except (OSError, ValueError):
            continue
        if (len(parts) >= 3 and parts[1].lower() == 'db_storage'
                and (parts[-1].lower().endswith('.db') or '.db-' in parts[-1].lower())):
            accounts.add(parts[0])
    if len(accounts) != 1:
        raise BindingError('无法唯一核对微信进程打开的数据库；请仅登录机器人号后重试。')
    return accounts.pop()


def binding_code_seen(rows, code, wxid, baseline):
    return any(str(row.get('content', '')).strip() == code
               and row.get('sender_username') == wxid
               and int(row.get('seq') or row.get('sort_seq') or 0) > baseline
               for row in rows)


class BindingGuard:
    """Latch a failed identity check; never recover by selecting another window."""
    def __init__(self, binding, proof, probe, runtime_probe=None):
        self.binding, self.proof, self.probe = binding, dict(proof), probe
        self.runtime_probe = runtime_probe
        self.failure = None

    def check(self):
        if self.failure:
            raise BindingError(self.failure)
        try:
            current = self.probe()
            if current != self.proof or current.get('account') != self.binding.account:
                raise BindingError('微信账号、进程或窗口发生变化，请重新绑定后启动桥。')
        except Exception as error:
            self.failure = str(error) if isinstance(error, BindingError) else '无法验证微信进程身份，已暂停操作。'
            raise BindingError(self.failure) from error
        return current

    def check_runtime(self):
        """Cheap window/process check while traversing a UI tree.

        Full opened-database proof is still required at DB and input boundaries.
        """
        if self.runtime_probe is None:
            return self.check()
        if self.failure:
            raise BindingError(self.failure)
        try:
            current = self.runtime_probe()
            expected = {k: v for k, v in self.proof.items() if k != 'account'}
            if current != expected:
                raise BindingError('绑定窗口或进程已改变。')
        except Exception as error:
            self.failure = '绑定窗口或进程已失效，请重新绑定后启动桥。'
            raise BindingError(self.failure) from error
        return self.proof


class BoundDatabase:
    """Pin database identity independently from the sending desktop instance.

    A lost GUI target must stop sending, not database reads. The optional guard
    parameter is accepted for older callers but is intentionally never consulted.
    """
    def __init__(self, db, binding, guard=None):
        self._db, self._binding = db, binding
        self._failure = None
        self._check()

    def _check(self):
        if self._failure:
            raise BindingError(self._failure)
        try:
            verify_database(self._binding, self._db)
        except BindingError as error:
            self._failure = str(error)
            raise

    def __getattr__(self, name):
        value = getattr(self._db, name)
        if not callable(value):
            return value
        def checked(*args, **kwargs):
            self._check()
            result = value(*args, **kwargs)
            self._check()
            return result
        return checked
