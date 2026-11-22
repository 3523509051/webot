"""Read-only identity observations, separate from GUI display-name matching."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import xml.etree.ElementTree as ET
import zlib


def bridge_id(wxid):return zlib.crc32(str(wxid).encode('utf-8'))%10**9


class IdentityDirectory:
    def __init__(self,path,account):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.account=account
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript('''CREATE TABLE IF NOT EXISTS people(gid TEXT,wxid TEXT,name TEXT,
                PRIMARY KEY(gid,wxid));
                CREATE TABLE IF NOT EXISTS aliases(gid TEXT,wxid TEXT,name TEXT,source TEXT,ts REAL,
                PRIMARY KEY(gid,wxid,name,source));
                CREATE TABLE IF NOT EXISTS message_map(session TEXT,mid INTEGER,data TEXT,ts REAL,
                PRIMARY KEY(session,mid));''')

    def update_members(self,gid,members):
        members=list(members)
        if not members:return  # keep the last known snapshot on an empty/failed DB read
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('DELETE FROM people WHERE gid=?',(gid,))
            for member in members:
                wxid=str(member.get('username') or member.get('wxid') or '')
                name=str(member.get('nick_name') or member.get('nickname') or member.get('remark') or '')
                if not wxid:continue
                c.execute('INSERT OR REPLACE INTO people VALUES(?,?,?)',(gid,wxid,name or wxid))
                for key in ('nick_name','nickname','remark','display_name','room_nickname'):
                    alias=str(member.get(key) or '').strip()
                    if alias:c.execute('INSERT OR REPLACE INTO aliases VALUES(?,?,?,?,?)',(gid,wxid,alias,'directory:'+key,time.time()))

    def observe_reference(self,gid,text):
        start=text.find('<msg')
        if start<0:return
        try:root=ET.fromstring(text[start:])
        except ET.ParseError:return
        with closing(sqlite3.connect(self.path)) as c, c:
            for ref in root.iter('refermsg'):
                wxid=ref.findtext('chatusr') or ''
                name=ref.findtext('displayname') or ''
                if wxid and name and not wxid.endswith('@chatroom'):
                    c.execute('INSERT OR REPLACE INTO aliases VALUES(?,?,?,?,?)',(gid,wxid,name,'reference',time.time()))

    def directory(self,gid):
        with closing(sqlite3.connect(self.path)) as c, c:
            members=[]
            for wxid,name in c.execute('SELECT wxid,name FROM people WHERE gid=?',(gid,)):
                aliases=[{'name':r[0],'source':r[1]} for r in c.execute('SELECT name,source FROM aliases WHERE gid=? AND wxid=?',(gid,wxid))]
                members.append({'wxid':wxid,'user_id':bridge_id(wxid),'nickname':name,'aliases':aliases})
            ids=[m['user_id'] for m in members]
            if len(set(ids))!=len(ids):raise ValueError('bridge user ID collision')
            return {'account':self.account,'group_wxid':gid,'group_id':bridge_id(gid),'members':members}

    def register_message(self,session,mid,gid,message):
        source=str(message.get('local_id') or message.get('seq') or '')
        wxid=str(message.get('sender_wxid') or '')
        key=hashlib.sha256(json.dumps([self.account,gid,source,message.get('seq')],ensure_ascii=False).encode()).hexdigest()
        self.observe_reference(gid,str(message.get('text') or ''))
        result={'account':self.account,'group_wxid':gid,'sender_wxid':wxid,
                'sender_name':message.get('sender_name'),'source_key':key,'session':session,
                'message_id':mid,'local_id':message.get('local_id'),'seq':message.get('seq')}
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('INSERT OR REPLACE INTO message_map VALUES(?,?,?,?)',(session,mid,json.dumps(result,ensure_ascii=False),time.time()))
        return result

    def message(self,session,mid):
        with closing(sqlite3.connect(self.path)) as c, c:
            r=c.execute('SELECT data FROM message_map WHERE session=? AND mid=?',(session,int(mid))).fetchone()
            return json.loads(r[0]) if r else None
