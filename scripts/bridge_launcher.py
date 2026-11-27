"""Background supervisor for webot only. Never starts or stops Pero/WeChat."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from local_process import InstanceLock,LauncherError,atomic_write,locked

ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'data/bridge-launcher'

def running():return locked(STATE/'instance.lock')

def environment():
    env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUNBUFFERED':'1'}
    path=ROOT/'data/connection.dpapi'
    if path.is_file():
        import win32crypt
        try:
            values=json.loads(win32crypt.CryptUnprotectData(path.read_bytes(),None,None,None,0)[1])
            if set(values)-{'PERO_MANAGEMENT_TOKEN','PERO_ONEBOT_TOKEN'}:raise ValueError()
            env.update({key:str(value) for key,value in values.items() if not env.get(key)})
        except Exception:raise LauncherError('无法读取本机连接凭据，请停止双方服务并重新运行“连接Pero.cmd”。') from None
    return env

def supervise():
    lock=InstanceLock(STATE/'instance.lock')
    if not lock.acquire():return 0
    job=None;state=None
    try:
        from windows_job import WindowsJob
        run_id=secrets.token_hex(8);run=STATE/'runs'/run_id;run.mkdir(parents=True)
        log=ROOT/'logs/bridge'/run_id/'bridge.log'
        job=WindowsJob()
        child=job.spawn([sys.executable,str(ROOT/'bridge/run_managed.py'),
                         '--stop-file',str(run/'stop'),'--log',str(log)],cwd=ROOT/'bridge',env=environment())
        state={'run_id':run_id,'pid':os.getpid(),'bridge_pid':child.pid,'log':str(log)}
        atomic_write(STATE/'session.json',json.dumps(state).encode())
        while child.poll() is None:
            request=STATE/'stop-request.json'
            try:stop=request.exists() and json.loads(request.read_text())['run_id']==run_id
            except (OSError,ValueError,KeyError):stop=False
            if stop:
                (run/'stop').touch()
                deadline=time.monotonic()+30
                while child.poll() is None and time.monotonic()<deadline:time.sleep(.2)
                return 0
            time.sleep(.2)
        return child.poll() or 0
    finally:
        if job:job.close()
        if state:
            (STATE/'session.json').unlink(missing_ok=True)
            (STATE/'stop-request.json').unlink(missing_ok=True)
        lock.close()

def main(argv=None):
    parser=argparse.ArgumentParser(description='只管理 webot 微信桥')
    parser.add_argument('action',choices=['start','supervise','stop','status'])
    parser.add_argument('--json',action='store_true',help='以 JSON 输出桥进程状态')
    args=parser.parse_args(argv)
    if args.action=='supervise':return supervise()
    if args.action=='start':
        if (ROOT/'data/launcher.lock').exists() and locked(ROOT/'data/launcher.lock'):
            raise LauncherError('检测到旧组合实例，请先运行 bridge/.venv/Scripts/python.exe scripts/stop_old_combination.py 收尾，再分别启动双方。')
        if running():print('webot 已在后台运行。');return 0
        if (ROOT/'bridge/cache/bridge-instance.lock').exists() and locked(ROOT/'bridge/cache/bridge-instance.lock'):
            raise LauncherError('已有其他入口启动的微信桥，请先正常停止该实例。')
        # Verify local credentials can be read, without touching Pero's process.
        environment()
        logs=ROOT/'logs/bridge';logs.mkdir(parents=True,exist_ok=True)
        with (logs/'supervisor.log').open('ab',buffering=0) as stream:
            child=subprocess.Popen([sys.executable,__file__,'supervise'],cwd=ROOT,
                stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,
                creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if running() and (STATE/'session.json').exists() and locked(ROOT/'bridge/cache/bridge-instance.lock'):
                print('webot 已在后台启动；此入口不会启动 Pero 或微信。连接状态请查看桥日志或 Pero 控制室。')
                return 0
            if child.poll() is not None and not running():raise LauncherError('webot 启动失败，请查看 logs/bridge 中的日志。')
            time.sleep(.3)
        raise LauncherError('尚未确认桥初始化；请查看 logs/bridge，或运行 status.cmd。')
    if not running():
        print(json.dumps({'running':False}) if args.json else 'webot 当前未运行。');return 0
    state=json.loads((STATE/'session.json').read_text())
    if args.action=='status':print(json.dumps({'running':True,**state},ensure_ascii=False,indent=2));return 0
    atomic_write(STATE/'stop-request.json',json.dumps({'run_id':state['run_id']}).encode())
    deadline=time.monotonic()+40
    while running() and time.monotonic()<deadline:time.sleep(.3)
    if running():raise LauncherError('webot 停止尚未完成，请检查桥日志。')
    print('webot 已停止；Pero 和微信未受影响。');return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (LauncherError,OSError,ValueError) as error:
        print('webot 启动器：'+str(error),file=sys.stderr);raise SystemExit(1)
