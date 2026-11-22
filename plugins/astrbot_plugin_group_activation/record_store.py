"""Durable memory sources. Context windows and vector indexes are derived views."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


class RecordStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.transaction() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied REAL);
                INSERT OR IGNORE INTO schema_versions VALUES(1, unixepoch());
                CREATE TABLE IF NOT EXISTS groups(gid TEXT PRIMARY KEY, raw_id TEXT, account TEXT,
                    context_after INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS identities(pid TEXT PRIMARY KEY, account TEXT, wxid TEXT,
                    bridge_id TEXT, name TEXT, UNIQUE(account,wxid));
                CREATE TABLE IF NOT EXISTS aliases(gid TEXT, pid TEXT REFERENCES identities(pid),
                    name TEXT, source TEXT, first_ts REAL, last_ts REAL,
                    PRIMARY KEY(gid,pid,name,source));
                CREATE INDEX IF NOT EXISTS alias_names ON aliases(gid,name);
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, gid TEXT NOT NULL,
                    source_key TEXT UNIQUE, pid TEXT, name TEXT, text TEXT, ts REAL, data TEXT,
                    archived INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS message_group_time ON messages(gid,ts,id);
                CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, gid TEXT NOT NULL,
                    kind TEXT, pid TEXT, text TEXT, original TEXT, ts REAL, event_time TEXT,
                    explicit INTEGER, state TEXT DEFAULT 'active', replaces TEXT, version INTEGER DEFAULT 1,
                    source_ids TEXT, metadata TEXT, fingerprint TEXT, UNIQUE(gid,fingerprint));
                CREATE INDEX IF NOT EXISTS memory_group_person ON memories(gid,pid,ts);
                CREATE TABLE IF NOT EXISTS memory_subjects(memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
                    pid TEXT,PRIMARY KEY(memory_id,pid));
                CREATE TABLE IF NOT EXISTS summaries(id TEXT PRIMARY KEY,gid TEXT,title TEXT,text TEXT,
                    start_ts REAL,end_ts REAL,source_ids TEXT,ts REAL,state TEXT DEFAULT 'active');
                CREATE TABLE IF NOT EXISTS daily_indexes(gid TEXT,day TEXT,text TEXT,source_ids TEXT,
                    ts REAL,PRIMARY KEY(gid,day));
                CREATE TABLE IF NOT EXISTS index_jobs(id TEXT PRIMARY KEY,gid TEXT,object_id TEXT,
                    state TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_ts REAL DEFAULT 0,
                    generation INTEGER,external_id TEXT,error TEXT, UNIQUE(object_id));
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,gid TEXT,ts REAL,state TEXT,
                    message_ids TEXT,reply_id INTEGER);
                CREATE TABLE IF NOT EXISTS tool_events(id TEXT PRIMARY KEY,gid TEXT,request_id TEXT,
                    tool TEXT,object_id TEXT,status TEXT,summary TEXT,ts REAL,reply_id INTEGER,
                    data TEXT,generation INTEGER);
                CREATE INDEX IF NOT EXISTS tool_group_request ON tool_events(gid,request_id,ts);
                CREATE TABLE IF NOT EXISTS migrations(source TEXT PRIMARY KEY,ts REAL,report TEXT);
                CREATE TABLE IF NOT EXISTS deletion_marks(gid TEXT,scope TEXT,ts REAL,
                    PRIMARY KEY(gid,scope));
            ''')

    @contextmanager
    def transaction(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=10)
            c.row_factory = sqlite3.Row
            try:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute('PRAGMA synchronous=FULL')
                c.execute('PRAGMA foreign_keys=ON')
                c.execute('BEGIN IMMEDIATE')
                yield c
                c.commit()
            except BaseException:
                c.rollback()
                raise
            finally:
                c.close()

    def _group(self, c, gid):
        c.execute('INSERT OR IGNORE INTO groups(gid) VALUES(?)', (str(gid),))

    def generation(self, gid):
        with self.transaction() as c:
            self._group(c,gid)
            return c.execute('SELECT generation FROM groups WHERE gid=?',(gid,)).fetchone()[0]

    def append_message(self, gid, item):
        text = str(item.get('text') or '')
        with self.transaction() as c:
            self._group(c,gid)
            key = item.get('source_key') or 'local:' + uuid.uuid4().hex
            c.execute('INSERT OR IGNORE INTO messages(gid,source_key,pid,name,text,ts,data) VALUES(?,?,?,?,?,?,?)',
                      (gid,key,item.get('person_id'),item.get('user','?'),text,float(item.get('ts',time.time())),encode(item)))
            return c.execute('SELECT id FROM messages WHERE source_key=?',(key,)).fetchone()[0]

    @staticmethod
    def _message(row):
        result = json.loads(row['data'])
        result.update(id=row['id'], person_id=row['pid'], text=row['text'], user=row['name'], ts=row['ts'])
        return result

    def recent_messages(self, gid, limit=500, *, context=True):
        with self.transaction() as c:
            self._group(c,gid)
            after = c.execute('SELECT context_after FROM groups WHERE gid=?',(gid,)).fetchone()[0] if context else 0
            rows = c.execute('SELECT * FROM messages WHERE gid=? AND id>? ORDER BY id DESC LIMIT ?',
                             (gid,after,max(1,int(limit)))).fetchall()
            return [self._message(r) for r in reversed(rows)]

    def message_count(self,gid):
        with self.transaction() as c:
            return c.execute('SELECT count(*) FROM messages WHERE gid=?',(gid,)).fetchone()[0]

    def search_messages(self,gid,terms=(),limit=20,person_id=None,start=None,end=None):
        sql='SELECT * FROM messages WHERE gid=?';args=[gid]
        for key,value,op in (('pid',person_id,'='),('ts',start,'>='),('ts',end,'<')):
            if value is not None:
                sql+=f' AND {key}{op}?';args.append(value)
        if terms:
            sql+=' AND ('+' OR '.join('instr(lower(text),lower(?))>0' for _ in terms)+')';args.extend(terms)
        sql+=' ORDER BY id DESC LIMIT ?';args.append(max(1,int(limit)))
        with self.transaction() as c:
            return [self._message(r) for r in reversed(c.execute(sql,args).fetchall())]

    def reset_context(self,gid):
        with self.transaction() as c:
            self._group(c,gid)
            last=c.execute('SELECT coalesce(max(id),0) FROM messages WHERE gid=?',(gid,)).fetchone()[0]
            c.execute('UPDATE groups SET context_after=?,generation=generation+1 WHERE gid=?',(last,gid))
            c.execute("UPDATE index_jobs SET generation=(SELECT generation FROM groups WHERE gid=?) WHERE gid=? AND state='pending'",(gid,gid))

    def save_memory(self,gid,text,*,kind='knowledge',person_id=None,explicit=False,source_ids=(),
                    original=None,event_time='',replaces=None,metadata=None,recorded_at=None):
        text=str(text).strip()
        if not text:
            raise ValueError('记忆内容不能为空')
        if len(text)>20000:
            raise ValueError('单条记忆超过20000字，请拆分')
        normalized=' '.join(text.split()).casefold()
        fp=hashlib.sha256(encode([kind,person_id,normalized,replaces]).encode()).hexdigest()
        with self.transaction() as c:
            self._group(c,gid)
            old=c.execute('SELECT id,state FROM memories WHERE gid=? AND fingerprint=?',(gid,fp)).fetchone()
            if old and old['state']=='active':
                return {'status':'already_exists','id':old['id'],'text':text}
            if old:
                fp += ':'+uuid.uuid4().hex
            mid='M'+uuid.uuid4().hex
            ts=time.time() if recorded_at is None else recorded_at
            if replaces:
                prev=c.execute('SELECT * FROM memories WHERE id=? AND gid=? AND state=?',(replaces,gid,'active')).fetchone()
                if not prev:
                    raise ValueError('找不到可修改的有效记录')
                c.execute("UPDATE memories SET state='superseded' WHERE id=?",(replaces,))
                c.execute("UPDATE index_jobs SET state='obsolete' WHERE object_id=?",(replaces,))
            c.execute('''INSERT INTO memories(id,gid,kind,pid,text,original,ts,event_time,explicit,replaces,
                source_ids,metadata,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (mid,gid,kind,person_id,text,original or text,ts,event_time,int(explicit),replaces,
                 encode(list(source_ids)),encode(metadata or {}),fp))
            for subject in (metadata or {}).get('subject_ids',[]):
                c.execute('INSERT OR IGNORE INTO memory_subjects VALUES(?,?)',(mid,subject))
            generation=c.execute('SELECT generation FROM groups WHERE gid=?',(gid,)).fetchone()[0]
            c.execute('INSERT INTO index_jobs(id,gid,object_id,generation) VALUES(?,?,?,?)',(uuid.uuid4().hex,gid,mid,generation))
            return {'status':'saved_index_pending','id':mid,'text':text}

    def get_memory(self,mid):
        with self.transaction() as c:
            r=c.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone()
            return dict(r) if r else None

    def search_memories(self,gid,terms=(),*,person_id=None,kind=None,explicit=None,start=None,end=None,limit=20,include_inactive=False):
        sql="SELECT * FROM memories WHERE gid=? AND "+("state IN ('active','superseded','cancelled')" if include_inactive else "state='active'");args=[gid]
        if kind=='profile_fact':
            sql+=" AND (kind='profile_fact' OR json_extract(metadata,'$.category')='profile_fact')"
            if person_id is not None:
                sql+=" AND ((kind='profile_fact' AND pid=?) OR EXISTS (SELECT 1 FROM memory_subjects s WHERE s.memory_id=memories.id AND s.pid=?))"
                args.extend([person_id,person_id])
        elif person_id is not None:
            sql+=' AND pid=?';args.append(person_id)
        if kind is not None and kind!='profile_fact':sql+=' AND kind=?';args.append(kind)
        for key,value,op in (('explicit',explicit,'='),('ts',start,'>='),('ts',end,'<')):
            if value is not None:
                sql+=f' AND {key}{op}?';args.append(value)
        if terms:
            sql+=' AND ('+' OR '.join('instr(lower(text),lower(?))>0' for _ in terms)+')';args.extend(terms)
        sql+=' ORDER BY ts DESC LIMIT ?';args.append(limit)
        with self.transaction() as c:return [dict(r) for r in c.execute(sql,args)]

    def cancel_memory(self,gid,mid):
        with self.transaction() as c:
            row=c.execute("SELECT * FROM memories WHERE id=? AND gid=? AND state='active'",(mid,gid)).fetchone()
            if not row:raise ValueError('找不到可取消的记录')
            c.execute("UPDATE memories SET state='cancelled' WHERE id=?",(mid,))
            c.execute("UPDATE index_jobs SET state='obsolete' WHERE object_id=?",(mid,))
            return {'id':mid,'status':'cancelled','text':row['text']}

    def pending_jobs(self,limit=10):
        with self.transaction() as c:
            return [dict(r) for r in c.execute("SELECT j.* FROM index_jobs j JOIN groups g ON g.gid=j.gid WHERE j.state='pending' AND j.next_ts<=? AND j.generation=g.generation ORDER BY j.rowid LIMIT ?",(time.time(),limit))]

    def finish_job(self,jid,*,error='',external_id=''):
        with self.transaction() as c:
            c.execute('UPDATE index_jobs SET state=?,attempts=attempts+1,next_ts=?,error=?,external_id=? WHERE id=? AND state=?',
                      ('pending' if error else 'done',time.time()+300,error[:200],external_id,jid,'pending'))

    def archive_candidates(self,gid,limit=100):
        with self.transaction() as c:
            return [self._message(r) for r in c.execute('SELECT * FROM messages WHERE gid=? AND archived=0 ORDER BY id LIMIT ?',(gid,limit))]

    def commit_summary(self,gid,items,title,text,*,generation=None):
        if not items or not text.strip():return None
        ids=[i['id'] for i in items]
        sid='S'+hashlib.sha256(encode([gid,ids]).encode()).hexdigest()[:32]
        with self.transaction() as c:
            self._group(c,gid)
            current=c.execute('SELECT generation FROM groups WHERE gid=?',(gid,)).fetchone()[0]
            if generation is not None and generation!=current:return None
            exists=c.execute('SELECT id FROM summaries WHERE id=?',(sid,)).fetchone()
            if exists:return sid
            c.execute('INSERT INTO summaries(id,gid,title,text,start_ts,end_ts,source_ids,ts) VALUES(?,?,?,?,?,?,?,?)',
                      (sid,gid,title,text,min(i['ts'] for i in items),max(i['ts'] for i in items),encode(ids),time.time()))
            c.executemany('UPDATE messages SET archived=1 WHERE gid=? AND id=?',[(gid,i) for i in ids])
            c.execute('INSERT OR IGNORE INTO index_jobs(id,gid,object_id,generation) VALUES(?,?,?,?)',(uuid.uuid4().hex,gid,sid,current))
            return sid

    def summaries(self,gid,start=None,end=None,limit=20):
        sql="SELECT * FROM summaries WHERE gid=? AND state='active'";args=[gid]
        if start is not None:sql+=' AND end_ts>=?';args.append(start)
        if end is not None:sql+=' AND start_ts<?';args.append(end)
        with self.transaction() as c:return [dict(r) for r in c.execute(sql+' ORDER BY start_ts DESC LIMIT ?',args+[limit])]

    def forget(self,gid,scope):
        with self.transaction() as c:
            self._group(c,gid)
            c.execute('UPDATE groups SET generation=generation+1 WHERE gid=?',(gid,))
            c.execute('INSERT OR REPLACE INTO deletion_marks VALUES(?,?,?)',(gid,scope,time.time()))
            condition="(kind='profile_fact' OR json_extract(metadata,'$.category')='profile_fact')" if scope=='profile' else "kind IN ('knowledge','legacy')"
            c.execute(f"UPDATE memories SET state='deleted' WHERE gid=? AND {condition}",(gid,))
            if scope!='profile':c.execute("UPDATE summaries SET state='deleted' WHERE gid=?",(gid,))
            if scope=='profile':
                c.execute("UPDATE index_jobs SET state='obsolete' WHERE gid=? AND state='pending' AND object_id IN (SELECT id FROM memories WHERE gid=? AND state<>'active')",(gid,gid))
                c.execute("UPDATE index_jobs SET generation=(SELECT generation FROM groups WHERE gid=?) WHERE gid=? AND state='pending'",(gid,gid))
            else:
                c.execute("UPDATE index_jobs SET state='obsolete' WHERE gid=? AND state='pending'",(gid,))

    def purge_group(self,gid):
        with self.transaction() as c:
            self._group(c,gid)
            c.execute('UPDATE groups SET generation=generation+1 WHERE gid=?',(gid,))
            for table in ('messages','memories','summaries','tool_events','requests','daily_indexes','index_jobs'):
                c.execute(f'DELETE FROM {table} WHERE gid=?',(gid,))
            c.execute('INSERT OR REPLACE INTO deletion_marks VALUES(?,?,?)',(gid,'all',time.time()))

    def migration_done(self,source):
        with self.transaction() as c:return c.execute('SELECT 1 FROM migrations WHERE source=?',(source,)).fetchone() is not None

    def mark_migration(self,source,report):
        with self.transaction() as c:c.execute('INSERT OR REPLACE INTO migrations VALUES(?,?,?)',(source,time.time(),encode(report)))
