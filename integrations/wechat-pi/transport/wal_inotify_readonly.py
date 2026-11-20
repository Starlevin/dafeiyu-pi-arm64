#!/usr/bin/env python3
"""Read-only encrypted WAL write notifications, Linux inotify + metadata check.

Observes only existing approved WAL basenames. Never decrypts, opens databases,
reads message contents, or touches client processes. NOT a chat message event.
"""
from __future__ import annotations
import ctypes
import errno
import os
from pathlib import Path
import select
import struct
import time

MASK = 0x00000002 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200 | 0x00000400
IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
HEADER = struct.Struct("iIII")
_libc = ctypes.CDLL(None, use_errno=True)
_init1 = _libc.inotify_init1
_init1.argtypes = [ctypes.c_int]
_init1.restype = ctypes.c_int
_add_watch = _libc.inotify_add_watch
_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
_add_watch.restype = ctypes.c_int

def role_of(path: Path) -> str | None:
    name = path.name
    if name == "session.db-wal":
        return "session"
    if name.startswith(("message_", "biz_message_")) and name.endswith(".db-wal"):
        stem=name.split(".db-wal")[0]
        parts=stem.rsplit("_",1)
        if len(parts)==2 and parts[1].isdigit():
            return "message"
    return None

def fingerprint(path: Path):
    try:
        stat=path.stat()
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None

class ReadOnlyWalEvents:
    def __init__(self, paths):
        self.paths={}
        self.watches={}
        self.descriptors={}
        self.previous={}
        for p in paths:
            p=Path(p)
            role=role_of(p)
            if not role or not p.parent.is_dir():
                continue
            self.paths[str(p)]=role
            self.previous[str(p)]=fingerprint(p)
        fd=_init1(os.O_NONBLOCK|os.O_CLOEXEC)
        if fd<0:
            e=ctypes.get_errno()
            raise OSError(e,os.strerror(e))
        self.fd=fd
        try:
            for p in self.paths:
                folder=str(Path(p).parent)
                if folder in self.descriptors:
                    continue
                wd=_add_watch(fd,os.fsencode(folder),MASK)
                if wd<0:
                    e=ctypes.get_errno()
                    raise OSError(e,os.strerror(e),folder)
                self.watches[wd]=folder
                self.descriptors[folder]=wd
        except BaseException:
            self.close()
            raise

    def poll(self, timeout=1.0):
        """Return set of WAL roles whose file fingerprint changed."""
        changed=set()
        if not self.paths:
            time.sleep(min(timeout,0.5))
            return changed
        has_event,_,_=select.select([self.fd],[],[],max(0.0,timeout))
        if not has_event:
            return changed
        try:
            data=os.read(self.fd,65536)
        except BlockingIOError:
            return changed
        pos=0
        while pos+HEADER.size<=len(data):
            wd,mask,cookie,name_len=HEADER.unpack_from(data,pos)
            pos+=HEADER.size
            raw=data[pos:pos+name_len]
            pos+=name_len
            if mask & 0x00004000: # IN_Q_OVERFLOW: uncertain, fallback to UI full scan
                changed.update(("session","message"))
                continue
            folder=self.watches.get(wd)
            if folder is None:
                continue
            name=os.fsdecode(raw.split(b"\x00",1)[0])
            target=os.path.join(folder,name)
            if target not in self.paths:
                continue
            cur=fingerprint(Path(target))
            if cur!=self.previous.get(target) or mask & (IN_CREATE|IN_DELETE|IN_MOVED_TO|IN_MOVED_FROM):
                self.previous[target]=cur
                changed.add(self.paths[target])
        return changed

    def close(self):
        if getattr(self,"fd",-1)>=0:
            os.close(self.fd)
            self.fd=-1

    def __enter__(self): return self
    def __exit__(self,*args): self.close()

if __name__=="__main__":
    from wal_hint_readonly import find_active_wal_files
    paths=find_active_wal_files()
    print("WATCHED_ENCRYPTED_WAL_COUNT",len(paths),flush=True)
    counts={"session":0,"message":0}
    t0=time.monotonic()
    with ReadOnlyWalEvents(paths) as watcher:
        while time.monotonic()-t0<20:
            for kind in watcher.poll(timeout=0.5):
                counts[kind]+=1
    print("OBSERVATION_SECONDS",round(time.monotonic()-t0,1),flush=True)
    print("METADATA_EVENTS_ONLY",counts,flush=True)
    print("DATABASES_NEVER_OPENED",True,flush=True)
