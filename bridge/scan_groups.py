"""Read the already logged-in account and all groups; never launch WeChat."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def scan():
    from bridge_lock import require_client_running
    require_client_running()
    from paths import find_xwechat_root
    from wechatauto import WeChatDB
    from account_binding import load_binding, verify_database, BoundDatabase
    from wechat_desktop import Desktop
    binding=load_binding()
    root=find_xwechat_root()
    if root and Path(root).resolve()!=Path(binding.db_dir).resolve():
        raise ValueError('binding_db_root_mismatch')
    guard=Desktop().guard(binding)
    raw_db=WeChatDB(db_dir=binding.db_dir,account=binding.account)
    verify_database(binding,raw_db)
    db=BoundDatabase(raw_db,binding,guard)
    info=db.get_self_info()
    if not isinstance(info,dict):raise ValueError('account_unavailable')
    account=str(info.get('wxid') or info.get('username') or info.get('user_name') or getattr(db,'wxid','') or '').strip()
    if not account or account.endswith('@chatroom'):raise ValueError('account_unavailable')
    groups=[]
    for row in db.get_groups() or []:
        if not isinstance(row,dict):raise ValueError('invalid_group')
        gid=str(row.get('username') or row.get('wxid') or row.get('id') or '').strip()
        name=str(row.get('nick_name') or row.get('nickname') or row.get('remark') or '').strip()
        if not name and gid:name=str(db.group_id_to_name(gid) or '').strip()
        if not gid.endswith('@chatroom') or not name:raise ValueError('group_identity_or_name_unavailable')
        groups.append({'id':gid,'name':name})
    if not groups:raise ValueError('empty_group_scan')
    return {'account_wxid':account,'groups':groups}


def main():
    parser=argparse.ArgumentParser(description='只读扫描已登录微信，不启动客户端')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    from bridge_lock import BridgeLock
    ownership=BridgeLock()
    if not ownership.acquire():
        print('此 webot 目录已有桥运行，请先停止原桥；不会接管或重复启动。',file=sys.stderr)
        return 1
    try:
        result=scan()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=args.output.parent,prefix='groups-',suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False)
            os.replace(name,args.output)
        finally:Path(name).unlink(missing_ok=True)
        print('已扫描群数量：'+str(len(result['groups'])))
        return 0
    except Exception as exc:
        from account_binding import BindingError
        detail=str(exc) if isinstance(exc,BindingError) else '请核对微信登录、数据根目录、账号绑定和依赖。'
        print('扫描失败（'+type(exc).__name__+'）：'+detail,file=sys.stderr)
        return 1
    finally:ownership.close()


if __name__=='__main__':raise SystemExit(main())
