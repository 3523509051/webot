"""Interactive, local-only account binding; never starts WeChat or sends text."""
import argparse
import secrets
import time
from pathlib import Path
from account_binding import (AccountBinding, BindingError, DEFAULT_BINDING, load_binding,
                             save_binding, self_wxid, verify_database, binding_code_seen)


def filehelper_rows(db):
    # Resolve real_sender_id from each message shard's Name2Id. The upstream
    # get_messages uses a different table and cannot reliably identify self.
    def read(tables):
        result = []
        for connection, table in tables:
            rows = db._shard_rows([(connection, table)], '',
                                 order_ext=db._MSG_ORDER_DESC, per_shard_limit=10)
            for row in rows:
                sender = connection.execute('SELECT user_name FROM Name2Id WHERE rowid=?',
                                            (row['real_sender_id'],)).fetchone()
                item = db._msg_row_to_dict(row)
                item['sender_username'] = str(sender[0] or '') if sender else ''
                result.append(item)
        return result
    return db._run_msg_query('filehelper', read) or []


def main(argv=None):
    parser = argparse.ArgumentParser(description='在微信所在桌面绑定机器人号；不发送消息、不启动微信。')
    parser.add_argument('--check', action='store_true', help='只检查现有绑定')
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args(argv)
    from wechat_desktop import Desktop
    from bridge_lock import BridgeLock
    ownership = BridgeLock()
    if not ownership.acquire():
        print('请先停止本目录的微信桥，再检查或更新账号绑定。')
        return 1
    try:
        desktop = Desktop()
        from wechatauto import WeChatDB
        if args.check:
            binding = load_binding()
            guard = desktop.guard(binding)
            db = WeChatDB(db_dir=binding.db_dir, account=binding.account)
            verify_database(binding, db)
            guard.check()
            print('数据库身份与桌面实例核对成功：', binding.wxid, binding.account)
            print('此检查未发送消息；GUI/UIA 收发仍需实机验证。')
            return 0
        from paths import find_xwechat_root
        root = find_xwechat_root()
        if not root:
            raise BindingError('找不到微信数据根目录，请先配置 wechat.db_dir。')
        windows = desktop.windows()
        if len(windows) != 1:
            raise BindingError('请仅保留一个机器人微信主窗口并登录，退出另一个微信后重试。')
        proof = desktop.proof(windows[0], root)
        db = WeChatDB(db_dir=str(root), account=proof['account'])
        binding = AccountBinding(str(Path(root).resolve()), proof['account'], self_wxid(db.get_self_info())).validate()
        verify_database(binding, db)
        from account_binding import BindingGuard
        guard = BindingGuard(binding, proof, lambda: desktop.proof(proof['hwnd'], root))
        baseline = max((int(r.get('sort_seq') or 0) for r in filehelper_rows(db)), default=0)
        code = 'WEBOT-BIND-' + secrets.token_hex(8).upper()
        print('检测到账号目录：', binding.account)
        print('内部 wxid：', binding.wxid)
        print('请在当前机器人微信的“文件传输助手”手动发送下面这一整行：')
        print(code, flush=True)
        print('等待验证；本工具不会代你发送消息，也不会连接 Pero。', flush=True)
        deadline = time.monotonic() + max(10, min(args.timeout, 600))
        while time.monotonic() < deadline:
            guard.check()
            verify_database(binding, db)
            if binding_code_seen(filehelper_rows(db), code, binding.wxid, baseline):
                guard.check()
                if input('已验证新消息来自此账号。确认将它绑定为机器人？输入 YES：').strip() != 'YES':
                    print('已取消，原绑定未修改。')
                    return 1
                guard.check()
                verify_database(binding, db)
                save_binding(DEFAULT_BINDING, binding)
                print('绑定已保存。启动桥后可再登录第二个微信；请先完成测试收发。')
                return 0
            time.sleep(1)
        raise BindingError('绑定码验证超时，原绑定未修改。请核对是否发在该账号的文件传输助手。')
    except (BindingError, ImportError, OSError) as error:
        print('绑定未完成：', str(error))
        return 1
    finally:
        ownership.close()


if __name__ == '__main__':
    raise SystemExit(main())
