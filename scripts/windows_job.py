"""Own only launcher-created processes, including the Windows venv child process.

Create children suspended, assign to a kill-on-close job, then resume. See the
Microsoft CreateProcessW, Job Objects and ResumeThread documentation.
"""
import ctypes
from ctypes import wintypes as w
import subprocess


class BasicLimits(ctypes.Structure):
    _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
              ('flags',w.DWORD),('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),
              ('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]


class IOCounters(ctypes.Structure):
    _fields_=[(name,ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]


class ExtendedLimits(ctypes.Structure):
    _fields_=[('basic',BasicLimits),('io',IOCounters),('process_memory',ctypes.c_size_t),
              ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]


class StartupInfo(ctypes.Structure):
    _fields_=[('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
              ('x',w.DWORD),('y',w.DWORD),('width',w.DWORD),('height',w.DWORD),
              ('xchars',w.DWORD),('ychars',w.DWORD),('fill',w.DWORD),('flags',w.DWORD),
              ('show',w.WORD),('reserved_bytes',w.WORD),('reserved_pointer',ctypes.c_void_p),
              ('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]


class ProcessInfo(ctypes.Structure):
    _fields_=[('process',w.HANDLE),('thread',w.HANDLE),('pid',w.DWORD),('tid',w.DWORD)]


class WindowsJob:
    def __init__(self):
        self.api=ctypes.WinDLL('kernel32',use_last_error=True)
        signatures={
            'CreateJobObjectW':([ctypes.c_void_p,w.LPCWSTR],w.HANDLE),
            'SetInformationJobObject':([w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD],w.BOOL),
            'AssignProcessToJobObject':([w.HANDLE,w.HANDLE],w.BOOL),
            'CreateProcessW':([w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,w.DWORD,
                               ctypes.c_void_p,w.LPCWSTR,ctypes.POINTER(StartupInfo),ctypes.POINTER(ProcessInfo)],w.BOOL),
            'ResumeThread':([w.HANDLE],w.DWORD),
            'TerminateProcess':([w.HANDLE,w.UINT],w.BOOL),
            'WaitForSingleObject':([w.HANDLE,w.DWORD],w.DWORD),
            'GetExitCodeProcess':([w.HANDLE,ctypes.POINTER(w.DWORD)],w.BOOL),
            'CloseHandle':([w.HANDLE],w.BOOL),
        }
        for name,(args,result) in signatures.items():
            function=getattr(self.api,name);function.argtypes=args;function.restype=result
        self.handle=self.api.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        limits=ExtendedLimits();limits.basic.flags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            error=ctypes.get_last_error();self.close();raise ctypes.WinError(error)
        self.children=[]

    def spawn(self,args,*,cwd,env):
        startup=StartupInfo();startup.cb=ctypes.sizeof(startup);info=ProcessInfo()
        command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(arg) for arg in args]))
        block=ctypes.create_unicode_buffer('\0'.join(key+'='+value for key,value in sorted(env.items(),key=lambda pair:pair[0].upper()))+'\0\0')
        # No inherited handles: children cannot keep the ownership job alive.
        flags=0x4|0x400|0x08000000  # SUSPENDED | UNICODE_ENVIRONMENT | NO_WINDOW
        if not self.api.CreateProcessW(str(args[0]),command,None,None,False,flags,block,str(cwd),ctypes.byref(startup),ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle,info.process):raise ctypes.WinError(ctypes.get_last_error())
            if self.api.ResumeThread(info.thread)==0xFFFFFFFF:raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.api.TerminateProcess(info.process,1);self.api.CloseHandle(info.process);raise
        finally:self.api.CloseHandle(info.thread)
        child=ChildProcess(self.api,info.process,int(info.pid));self.children.append(child);return child

    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None
        for child in getattr(self,'children',[]):child.close()


class ChildProcess:
    def __init__(self,api,handle,pid):self.api,self.handle,self.pid=api,handle,pid
    def poll(self):
        if self.api.WaitForSingleObject(self.handle,0)==258:return None
        code=w.DWORD()
        if not self.api.GetExitCodeProcess(self.handle,ctypes.byref(code)):raise ctypes.WinError(ctypes.get_last_error())
        return int(code.value)
    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None
