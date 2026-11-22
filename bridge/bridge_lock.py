"""One bridge owns the desktop target; no process-name killing."""
from pathlib import Path
import msvcrt
import csv
import os
import subprocess


def require_client_running():
    """Check processes only; never spawn a WeChat executable or login UI."""
    binary=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/tasklist.exe'
    result=subprocess.run([str(binary),'/FO','CSV','/NH'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    names={row[0].strip().lower() for row in csv.reader(result.stdout.decode(errors='replace').splitlines()) if row}
    if result.returncode or not names.intersection({'weixin.exe','wechat.exe'}):
        raise RuntimeError('wechat_not_running_manual_start_required')


class BridgeLock:
    def __init__(self):self.file=None
    def acquire(self):
        path=Path(__file__).resolve().parent/'cache/bridge-instance.lock'
        path.parent.mkdir(parents=True,exist_ok=True)
        self.file=path.open('a+b');self.file.seek(0,2)
        if not self.file.tell():self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1);return True
        except OSError:self.file.close();self.file=None;return False
    def close(self):
        if self.file:
            self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            self.file.close();self.file=None
