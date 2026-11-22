"""Identity resolution is exact and evidence-based, never fuzzy name merging."""
import time
import json


class IdentityResolver:
    def __init__(self,store):self.store=store

    def ingest(self,gid,directory):
        account=str(directory.get('account') or 'default')
        now=time.time()
        with self.store.transaction() as c:
            self.store._group(c,gid)
            c.execute('UPDATE groups SET raw_id=?,account=? WHERE gid=?',(directory.get('group_wxid'),account,gid))
            for member in directory.get('members',[]):
                wxid=str(member.get('wxid') or '')
                if not wxid:continue
                pid=account+':'+wxid
                name=str(member.get('nickname') or wxid)
                c.execute('INSERT INTO identities(pid,account,wxid,bridge_id,name) VALUES(?,?,?,?,?) '
                          'ON CONFLICT(pid) DO UPDATE SET bridge_id=excluded.bridge_id,name=excluded.name',
                          (pid,account,wxid,str(member.get('user_id') or ''),name))
                aliases=[{'name':name,'source':'member_directory'},*(member.get('aliases') or [])]
                for alias in aliases:
                    text=str(alias.get('name') or '').strip()
                    if not text:continue
                    source=str(alias.get('source') or 'member_directory')
                    c.execute('INSERT INTO aliases VALUES(?,?,?,?,?,?) ON CONFLICT(gid,pid,name,source) '
                              'DO UPDATE SET last_ts=excluded.last_ts',(gid,pid,text,source,now,now))
            # Bind imported profiles only when the name has exactly one evidenced identity in this group.
            for row in c.execute('SELECT name,min(pid) AS pid,count(DISTINCT pid) AS n FROM aliases WHERE gid=? GROUP BY name',(gid,)).fetchall():
                if row['n']==1:
                    c.execute('UPDATE memories SET pid=? WHERE gid=? AND pid=?',(row['pid'],gid,'legacy:'+row['name']))

    def resolve(self,gid,name='',bridge_user_id=None):
        with self.store.transaction() as c:
            if bridge_user_id is not None:
                rows=c.execute('SELECT DISTINCT i.* FROM identities i JOIN aliases a ON a.pid=i.pid '
                               'WHERE a.gid=? AND i.bridge_id=?',(gid,str(bridge_user_id))).fetchall()
            else:
                rows=c.execute('SELECT DISTINCT i.* FROM identities i JOIN aliases a ON a.pid=i.pid '
                               'WHERE a.gid=? AND a.name=? COLLATE NOCASE',(gid,name.strip())).fetchall()
            people=[]
            for row in rows:
                d=dict(row)
                d['aliases']=[r[0] for r in c.execute('SELECT DISTINCT name FROM aliases WHERE gid=? AND pid=?',(gid,d['pid']))]
                people.append(d)
            return {'status':'found' if len(people)==1 else 'ambiguous' if people else 'not_found','people':people}

    def names(self,gid,pid):
        with self.store.transaction() as c:return [r[0] for r in c.execute('SELECT DISTINCT name FROM aliases WHERE gid=? AND pid=?',(gid,pid))]

    def bind_message_sources(self,gid,observations,max_delta=30):
        """Legacy names alone are insufficient: match actual text/time and reject competing senders."""
        by_text={}
        for item in observations:
            if item.get('pid') and item.get('text'):by_text.setdefault(item['text'],[]).append(item)
        count=0
        with self.store.transaction() as c:
            rows=c.execute('SELECT id,text,ts,data FROM messages WHERE gid=? AND pid IS NULL',(gid,)).fetchall()
            for row in rows:
                candidates=[i for i in by_text.get(row['text'],[]) if abs(i['ts']-row['ts'])<=max_delta]
                pids={i['pid'] for i in candidates}
                if len(pids)!=1:continue
                data=json.loads(row['data']);data.update(legacy_unresolved=False,identity_source='wechat_history_exact_text_time')
                c.execute('UPDATE messages SET pid=?,data=? WHERE id=?',(next(iter(pids)),json.dumps(data,ensure_ascii=False),row['id']))
                count+=1
        return count
