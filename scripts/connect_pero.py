"""Explicit one-time connection setup; starts no services."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from local_process import atomic_write,locked,LauncherError

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description='双方停止时同步全部群路由和本机连接凭据，不启动服务')
    parser.add_argument('--pero-root',type=Path,default=ROOT.parent/'Pero')
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args();pero=args.pero_root.resolve()
    if not args.worker:
        python=pero/'.venv/Scripts/python.exe'
        if not python.is_file():raise LauncherError('请先安装 Pero 环境。')
        return subprocess.call([str(python),__file__,'--worker','--pero-root',str(pero)],cwd=ROOT)
    if locked(ROOT/'data/bridge-launcher/instance.lock') or locked(ROOT/'data/launcher.lock') or locked(pero/'data/launcher.lock'):
        raise LauncherError('请先分别停止 Pero 和 webot，再运行连接配置；不会停止任何现有进程。')
    sys.path.insert(0,str(pero/'src'))
    from pero.manager.local_access import management_token,onebot_token
    from pero.manager.vault import DPAPI
    from pero.core.settings import load_settings
    from wechat_pairing import prepare
    config=pero/'config/local.json'
    if not config.exists():
        legacy=ROOT/'data/combo/config.json'
        atomic_write(config,(legacy if legacy.is_file() else pero/'config/default.json').read_bytes())
    settings=load_settings(config)
    # Do not modify a configuration used by an unowned/manual Core process.
    import socket
    with socket.socket() as client:
        client.settimeout(.3)
        if client.connect_ex(('127.0.0.1',settings.service.port))==0:
            raise LauncherError('Pero 核心端口仍在使用，请先停止该实例。')
    result=prepare(pero,ROOT,config,ROOT/'data/connection-state','')
    atomic_write(pero/'data/manager/scope-labels.json',Path(result['labels']).read_bytes())
    credentials={
        'PERO_MANAGEMENT_TOKEN':management_token(create=True,legacy_paths=[ROOT/'data/launcher/management-token.dpapi']),
        'PERO_ONEBOT_TOKEN':onebot_token(create=True,legacy_paths=[ROOT/'data/launcher/onebot-token.dpapi'])}
    atomic_write(ROOT/'data/connection.dpapi',DPAPI.transform(json.dumps(credentials).encode('utf-8')))
    print('连接配置已保存；凭据已按当前 Windows 用户加密，未启动任何服务。')
    print('接下来分别运行 Pero/start.cmd 和 webot/start.cmd。管理访问码由 Pero/login-code.cmd 查看。')
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except LauncherError as error:print(str(error),file=sys.stderr);raise SystemExit(1)
    except Exception:print('连接配置未完成。请检查双方已停止、安装依赖、微信目录和账号绑定。',file=sys.stderr);raise SystemExit(1)
