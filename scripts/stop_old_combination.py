"""Migration-only stop request; cannot launch either application."""
import argparse,json,time
from pathlib import Path
from local_process import locked,atomic_write,LauncherError
ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser(description='只停止旧组合实例，迁移后无需再使用')
    parser.add_argument('--state-dir',type=Path,default=ROOT/'data/launcher')
    args=parser.parse_args()
    if not (ROOT/'data/launcher.lock').exists() or not locked(ROOT/'data/launcher.lock'):
        print('没有旧组合实例在运行。');return 0
    state=json.loads((args.state_dir/'session.json').read_text(encoding='utf-8'))
    atomic_write(args.state_dir/'stop-request.json',json.dumps({'run_id':state['run_id']}).encode())
    print('已请求旧组合正常收尾；不会启动任何进程。')
    deadline=time.monotonic()+170
    while locked(ROOT/'data/launcher.lock') and time.monotonic()<deadline:time.sleep(.5)
    if locked(ROOT/'data/launcher.lock'):raise LauncherError('旧组合仍在收尾，请检查原启动日志。')
    print('旧组合已停止。');return 0
if __name__=='__main__':raise SystemExit(main())
