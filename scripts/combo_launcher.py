"""Windows source-checkout launcher, owned by webot, managing Pero and the bridge."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sys
import time
import urllib.request
import webbrowser

ROOT=Path(__file__).resolve().parents[1]
PERO_ROOT=Path(os.environ.get('WEBOT_PERO_ROOT',str(ROOT.parent/'Pero'))).resolve()
sys.path.insert(0,str(PERO_ROOT/'src'))


class LauncherError(Exception):pass


def atomic_write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with temporary.open('wb') as file:file.write(data);file.flush();os.fsync(file.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


class InstanceLock:
    def __init__(self,path):self.path=path;self.file=None
    def acquire(self):
        import msvcrt
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b');self.file.seek(0,2)
        if self.file.tell()==0:self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1);return True
        except OSError:self.file.close();self.file=None;return False
    def close(self):
        if self.file:
            import msvcrt
            self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1);self.file.close();self.file=None


def running(state_dir):
    path=ROOT/'data/launcher.lock'
    if not path.exists():return False
    lock=InstanceLock(path)
    if lock.acquire():lock.close();return False
    return True


def load_state(state_dir):
    try:return json.loads((state_dir/'session.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):raise LauncherError('无法读取启动状态；请检查启动窗口。') from None


def management_token(state_dir,*,create=False):
    override=os.environ.get('PERO_MANAGEMENT_TOKEN','').strip()
    return protected_token(state_dir/'management-token.dpapi',override,create=create)


def protected_token(path,override='',*,create=False):
    from pero.manager.vault import DPAPI,VaultError
    try:
        if override and create:
            atomic_write(path,DPAPI.transform(override.encode('utf-8')))
            return override
        if path.exists():return DPAPI.transform(path.read_bytes(),decrypt=True).decode('utf-8')
        if not create:raise LauncherError('尚未保存管理访问码，请先运行 start.cmd。')
        value=secrets.token_urlsafe(32)
        atomic_write(path,DPAPI.transform(value.encode('utf-8')))
        return value
    except (VaultError,UnicodeError,OSError):
        raise LauncherError('无法读取当前 Windows 用户加密的管理访问码；请使用原运行用户检查，不会覆盖旧密文。') from None


def request(url,path,*,token='',body=None):
    data=json.dumps(body).encode('utf-8') if body is not None else None
    headers={'Authorization':'Bearer '+token} if token else {}
    if data is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url+path,data=data,headers=headers)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=2) as response:
        return json.load(response)


def available(host,port):
    try:
        for family,kind,proto,_,address in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM):
            with socket.socket(family,kind,proto) as listener:
                if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                listener.bind(address)
    except OSError:raise LauncherError(f'地址 {host}:{port} 不可用或已被占用；请使用其它端口，不会接管或终止已有进程。') from None


def check(args):
    for module in ('uvicorn','fastapi','httpx','websockets'):
        try:__import__(module)
        except ImportError:raise LauncherError('缺少依赖，请先按 README 安装 requirements-lock.txt。') from None
    configured=args.config or os.environ.get('PERO_CONFIG_FILE')
    path=Path(configured).expanduser() if configured else ROOT/'data/combo/config.json'
    if not path.is_absolute():path=ROOT/path
    if not path.exists():
        if configured:raise LauncherError('指定的配置文件不存在：'+str(path))
        template=json.loads((PERO_ROOT/'config/default.json').read_text(encoding='utf-8-sig'))
        template.setdefault('memory',{})['root']=str(ROOT/'data/pero-data')
        atomic_write(path,json.dumps(template,ensure_ascii=False,indent=2).encode('utf-8'))
        print('已从默认配置创建 webot/data/combo/config.json，未覆盖其它配置。',flush=True)
    from pero.core.settings import load_settings
    try:settings=load_settings(path)
    except Exception as exc:raise LauncherError('配置校验失败（'+type(exc).__name__+'），请检查运行配置。') from None
    if not (PERO_ROOT/'frontend/dist/index.html').is_file():raise LauncherError('前端尚未构建，请进入 frontend 执行 npm ci 和 npm run build。')
    if not 1<=args.manager_port<=65535:raise LauncherError('控制室端口必须在 1–65535。')
    ports=[settings.service.port,args.manager_port]
    if settings.channels.onebot11.enabled:ports.append(settings.channels.onebot11.port)
    if len(ports)!=len(set(ports)):raise LauncherError('核心、控制室和启用的桥接通道端口不能重复。')
    return path.resolve(),settings


def health_check(url,path,service):
    try:
        result=request(url,path)
        return result.get('ok') is True and result.get('service')==service
    except Exception:return False


def stop_request(args,state_dir):
    if not running(state_dir):print('当前没有本脚本管理的运行实例。');return 0
    state=load_state(state_dir)
    atomic_write(state_dir/'stop-request.json',json.dumps({'run_id':state['run_id']}).encode())
    print('已请求停止，等待现有对话和后台任务收尾……',flush=True)
    deadline=time.monotonic()+args.stop_timeout+35
    while time.monotonic()<deadline:
        if not running(state_dir):print('核心和控制室已停止。');return 0
        time.sleep(0.5)
    raise LauncherError('尚未停止，请查看启动窗口；任务繁忙时不会强制终止。')


def drain(state,token,timeout):
    try:
        request(state['core_url'],'/api/v1/management/drain',token=token,body={'enabled':True})
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if not request(state['core_url'],'/api/v1/management/drain',token=token).get('busy',True):return True
            time.sleep(0.5)
    except Exception:
        # Do not force-stop a live, unverified core on a failed management call.
        return False
    try:request(state['core_url'],'/api/v1/management/drain',token=token,body={'enabled':False})
    except Exception:pass
    return False


def start(args,state_dir):
    lock=InstanceLock(ROOT/'data/launcher.lock')
    if not lock.acquire():
        state=load_state(state_dir)
        print('此目录的启动器已经运行：'+state['manager_url'])
        if not args.no_browser and health_check(state['manager_url'],'/manage/v1/health','pero-manager'):webbrowser.open(state['manager_url'])
        return 0
    job=None;state=None
    try:
        config,settings=check(args)
        available(settings.service.host,settings.service.port);available('127.0.0.1',args.manager_port)
        pairing=None
        if args.with_webot or args.webot_root:
            available('127.0.0.1',settings.channels.onebot11.port)
            from wechat_pairing import prepare
            webot=args.webot_root or ROOT.parent/'webot'
            try:
                pairing=prepare(PERO_ROOT,ROOT,config,state_dir,args.agent_id)
                from pero.core.settings import load_settings
                settings=load_settings(config)
            except LauncherError:raise
            except Exception as exc:raise LauncherError('微信组合配置失败（'+type(exc).__name__+'）；未启动微信。请检查扫描、账号与配置。') from None
        available(settings.service.host,settings.service.port);available('127.0.0.1',args.manager_port)
        if settings.channels.onebot11.enabled:available(settings.channels.onebot11.host,settings.channels.onebot11.port)
        token=management_token(state_dir,create=True)
        from windows_job import WindowsJob
        job=WindowsJob();run_id=secrets.token_hex(8);run_dir=state_dir/'runs'/run_id;run_dir.mkdir(parents=True)
        core_url=f'http://127.0.0.1:{settings.service.port}'
        if settings.service.host not in ('127.0.0.1','localhost','0.0.0.0','::','::1'):
            raise LauncherError('此本地启动器要求核心能通过本机回环访问；远程服务请单独部署。')
        if settings.service.host in ('::','::1'):core_url=f'http://[::1]:{settings.service.port}'
        if settings.service.host=='localhost':core_url=f'http://localhost:{settings.service.port}'
        manager_url=f'http://127.0.0.1:{args.manager_port}'
        log_dir=ROOT/'logs'/'launcher'/run_id
        env=dict(os.environ,PERO_MANAGEMENT_TOKEN=token,PERO_MANAGER_URL=manager_url,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
        if pairing:
            env[pairing['token_env']]=protected_token(state_dir/'onebot-token.dpapi',create=True)
        env['PYTHONPATH']=str(PERO_ROOT/'src')+(os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        children=[]
        for component in ('core','manager'):
            command=[sys.executable,str(ROOT/'scripts/launch_child.py'),component,'--config',str(config),
                     '--manager-port',str(args.manager_port),'--core-url',core_url,'--manager-state',str(args.manager_state_dir.resolve()),
                     '--stop-file',str(run_dir/'stop'),'--log',str(log_dir/(component+'.log'))]
            if pairing and component=='manager':command+=['--scope-labels',str(pairing['labels'])]
            children.append(job.spawn(command,cwd=PERO_ROOT,env=env))
        state={'run_id':run_id,'pid':os.getpid(),'core_pid':children[0].pid,'manager_pid':children[1].pid,
               'config':str(config),'core_url':core_url,'manager_url':manager_url,'logs':str(log_dir)}
        atomic_write(state_dir/'session.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
        deadline=time.monotonic()+args.start_timeout
        while time.monotonic()<deadline:
            if any(child.poll() is not None for child in children):raise LauncherError('服务启动失败，请检查日志：'+str(log_dir))
            if health_check(core_url,'/health','pero') and health_check(manager_url,'/manage/v1/health','pero-manager'):break
            time.sleep(0.3)
        else:raise LauncherError('启动健康检查超时，请检查日志：'+str(log_dir))
        if pairing:
            # Apply saved per-group switches before the bridge can produce events.
            deadline=time.monotonic()+args.start_timeout
            while time.monotonic()<deadline:
                if any(child.poll() is not None for child in children):raise LauncherError('服务在桥接前退出，请检查日志。')
                try:
                    applied=request(manager_url,'/manage/v1/config/state',token=token)
                    if applied.get('state')=='applied':break
                except Exception:pass
                time.sleep(0.3)
            else:raise LauncherError('已保存群设置尚未应用，未启动桥；请查看控制室配置同步日志。')
            command=[str(pairing['python']),str(pairing['webot_root']/'bridge/run_managed.py'),
                     '--stop-file',str(run_dir/'stop'),'--log',str(log_dir/'bridge.log')]
            bridge_env={key:value for key,value in env.items() if key not in ('PYTHONPATH',)}
            children.append(job.spawn(command,cwd=pairing['webot_root']/'bridge',env=bridge_env))
            state['bridge_pid']=children[-1].pid
            atomic_write(state_dir/'session.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
            deadline=time.monotonic()+args.start_timeout
            while time.monotonic()<deadline:
                if any(child.poll() is not None for child in children):raise LauncherError('桥启动失败，请检查 bridge.log；请手动启动并登录微信。')
                try:
                    if any(item.get('connected') for item in request(core_url,'/api/v1/channels').get('channels',[])):break
                except Exception:pass
                time.sleep(0.3)
            else:raise LauncherError('桥未连接，请核对微信、webot 依赖和 bridge.log。')
            print('微信桥已连接；微信客户端由你手动启动和登录。',flush=True)
        print('Pero 已启动。\n控制室：'+manager_url+'\n日志：'+str(log_dir)+'\n运行 login-code.cmd 查看管理访问码。\n请保留本窗口；用 stop.cmd 或 Ctrl+C 请求收尾停止。',flush=True)
        if os.environ.get('PERO_MANAGEMENT_TOKEN'):print('本次沿用环境变量中的管理访问码。',flush=True)
        if not args.no_browser:webbrowser.open(manager_url)
        requested=False
        def stop_signal(*_):
            nonlocal requested
            requested=True
        signal.signal(signal.SIGINT,stop_signal)
        while True:
            if any(child.poll() is not None for child in children):raise LauncherError('一个服务已退出，正在清理本次实例；请查看日志：'+str(log_dir))
            marker=state_dir/'stop-request.json'
            try:requested=requested or (marker.exists() and json.loads(marker.read_text())['run_id']==run_id)
            except (OSError,ValueError,KeyError):pass
            if requested:
                print('正在等待对话与后台任务收尾……',flush=True)
                if not drain(state,token,args.stop_timeout):
                    print('服务仍忙或无法确认，已取消本次停止请求，请稍后重试。',flush=True)
                    marker.unlink(missing_ok=True);requested=False;continue
                (run_dir/'stop').touch()
                deadline=time.monotonic()+30
                while time.monotonic()<deadline and any(child.poll() is None for child in children):time.sleep(0.25)
                print('Pero 已停止。',flush=True);return 0
            time.sleep(0.3)
    finally:
        if job:job.close()
        if state:
            (state_dir/'session.json').unlink(missing_ok=True)
            (state_dir/'stop-request.json').unlink(missing_ok=True)
        lock.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Pero Windows 本地启动器')
    parser.add_argument('action',choices=['start','stop','status','login-code','check','prepare'],nargs='?',default='start')
    parser.add_argument('--config',help='配置路径；默认 config/local.json，首次从 default.json 创建')
    parser.add_argument('--manager-port',type=int,default=8766)
    parser.add_argument('--state-dir',type=Path,default=ROOT/'data/launcher')
    parser.add_argument('--manager-state-dir',type=Path,default=ROOT/'data/manager',help='控制室状态目录，默认沿用手动启动目录')
    parser.add_argument('--start-timeout',type=float,default=60)
    parser.add_argument('--stop-timeout',type=float,default=120)
    parser.add_argument('--no-browser',action='store_true')
    parser.set_defaults(with_webot=True,webot_root=ROOT)
    parser.add_argument('--agent-id',default='',help='新增群绑定的 Agent，默认配置中的首个 Agent；已有绑定保持不变')
    args=parser.parse_args(argv)
    if sys.platform!='win32':raise LauncherError('此启动脚本用于 Windows；其它平台请使用 README 的手动启动入口。')
    state_dir=args.state_dir.expanduser().resolve()
    if not (0<args.start_timeout<=600 and 0<args.stop_timeout<=600):raise LauncherError('启动／停止等待时间需在 0–600 秒。')
    if args.action=='prepare':
        if running(state_dir):return 0
        config,settings=check(args)
        from wechat_pairing import prepare
        prepare(PERO_ROOT,ROOT,config,state_dir,args.agent_id)
        return 0
    if args.action=='start':return start(args,state_dir)
    if args.action=='check':check(args);print('配置、依赖和前端构建检查通过。');return 0
    if args.action=='login-code':
        print(management_token(state_dir));return 0  # Explicit user action only; never a log file.
    if args.action=='stop':return stop_request(args,state_dir)
    if not running(state_dir):print('当前没有本脚本管理的运行实例。');return 0
    state=load_state(state_dir)
    print(json.dumps({'running':True,'core_healthy':health_check(state['core_url'],'/health','pero'),
                     'manager_healthy':health_check(state['manager_url'],'/manage/v1/health','pero-manager'),
                     'config':state['config'],'manager_url':state['manager_url'],'logs':state['logs']},ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except LauncherError as exc:print('启动器：'+str(exc),file=sys.stderr);raise SystemExit(1)
    except KeyboardInterrupt:raise SystemExit(130)
