"""Local process ownership primitives; no Pero imports."""
import json
import os
from pathlib import Path
import secrets

class LauncherError(RuntimeError):pass

def atomic_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)

class InstanceLock:
    def __init__(self,path):self.path=Path(path);self.file=None
    def acquire(self):
        import msvcrt
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b');self.file.seek(0,2)
        if not self.file.tell():self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1);return True
        except OSError:self.close();return False
    def close(self):
        if self.file:self.file.close();self.file=None

def locked(path):
    lock=InstanceLock(path)
    if lock.acquire():lock.close();return False
    return True
