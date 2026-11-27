"""User-facing webot entry; a hidden supervisor owns only its child services."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT=Path(__file__).resolve().parents[1]

def healthy(url,path,service):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url+path,timeout=2) as response:
            value=json.load(response)
        return value.get('ok') is True and value.get('service')==service
    except Exception:return False

def bridge_connected(url):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url+'/api/v1/channels',timeout=2) as response:
            return any(row.get('connected') for row in json.load(response).get('channels',[]))
    except Exception:return False

def main(argv=None):
    parser=argparse.ArgumentParser(description='webot owns the local Pero + WeChat combination')
    parser.add_argument('action',choices=['start','stop','status','login-code','prepare','check'])
    parser.add_argument('--pero-root',type=Path,default=ROOT.parent/'Pero')
    args,extra=parser.parse_known_args(argv)
    pero=args.pero_root.expanduser().resolve()
    python=pero/'.venv/Scripts/python.exe'
    if not python.is_file():
        print('Install the adjacent Pero repository and its .venv first, or pass --pero-root PATH.',file=sys.stderr)
        return 1
    env={**os.environ,'WEBOT_PERO_ROOT':str(pero),'PYTHONIOENCODING':'utf-8'}
    command=[str(python),str(ROOT/'scripts/combo_launcher.py')]
    if args.action!='start':
        return subprocess.call(command+[args.action]+extra,cwd=ROOT,env=env)
    # Prepare is interactive and must succeed before creating a hidden process.
    result=subprocess.call(command+['prepare']+extra,cwd=ROOT,env=env)
    if result:return result
    logs=ROOT/'logs/combo'
    logs.mkdir(parents=True,exist_ok=True)
    with (logs/'supervisor.log').open('ab',buffering=0) as stream:
        child=subprocess.Popen(command+['start','--no-browser']+extra,cwd=ROOT,env=env,
                               stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,
                               creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP)
    start_timeout=60.0
    for i,value in enumerate(extra):
        if value=='--start-timeout' and i+1<len(extra):start_timeout=float(extra[i+1])
        elif value.startswith('--start-timeout='):start_timeout=float(value.split('=',1)[1])
    deadline=time.monotonic()+3*start_timeout+15
    # Custom state-dir is supported by the underlying CLI, but readiness must
    # inspect the same state path rather than the default.
    state_dir=ROOT/'data/launcher'
    for i,value in enumerate(extra):
        if value=='--state-dir' and i+1<len(extra):state_dir=Path(extra[i+1]).resolve()
        elif value.startswith('--state-dir='):state_dir=Path(value.split('=',1)[1]).resolve()
    while time.monotonic()<deadline:
        try:
            state=json.loads((state_dir/'session.json').read_text(encoding='utf-8'))
            manager=state['manager_url'];core=state['core_url']
            if state.get('bridge_pid') and healthy(core,'/health','pero') and healthy(manager,'/manage/v1/health','pero-manager') and bridge_connected(core):
                print('Core, control room and bridge process started. Check bridge connection in the control room.')
                print('Control room: '+manager)
                webbrowser.open(manager)
                return 0
        except (OSError,ValueError,KeyError):pass
        if child.poll() not in (None,0):
            print('Startup failed. See '+str(logs/'supervisor.log'),file=sys.stderr)
            return 1
        time.sleep(0.5)
    print('Readiness not confirmed. Use status.cmd and inspect '+str(logs/'supervisor.log'),file=sys.stderr)
    return 1

if __name__=='__main__':raise SystemExit(main())
