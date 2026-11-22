"""Bounded, recoverable summaries of fixed source-message snapshots."""
from datetime import datetime
import re
import time
import hashlib
from .memory_service import TZ
from .record_store import encode


class Journal:
    def __init__(self,store,summarize,*,max_chars=6000):
        self.store=store;self.summarize=summarize;self.max_chars=max_chars
        self._daily_versions={}

    async def archive(self,gid,force=False):
        candidates=self.store.archive_candidates(gid,200)
        if not candidates:return []
        if not force and len(candidates)<100 and not (len(candidates)>=20 and time.time()-candidates[-1]['ts']>=300):return []
        generation=self.store.generation(gid)
        # One long source is split, but is marked covered only after all parts succeed.
        chunks=[];current=[];body=''
        for item in candidates:
            day=datetime.fromtimestamp(item['ts'],TZ).date()
            if current and day!=datetime.fromtimestamp(current[-1]['ts'],TZ).date():
                chunks.append((current,body));current=[];body=''
            person=hashlib.sha256(item['person_id'].encode()).hexdigest()[:12] if item.get('person_id') else '待核实'
            line=f"[原文{item['id']} {item['user']} 身份:{person} {datetime.fromtimestamp(item['ts'],TZ).isoformat()}] {item['text']}\n"
            if len(line)>self.max_chars:
                if current:chunks.append((current,body));current=[];body=''
                for offset in range(0,len(line),self.max_chars):chunks.append(([item],line[offset:offset+self.max_chars]))
            else:
                if current and len(body)+len(line)>self.max_chars:chunks.append((current,body));current=[];body=''
                current.append(item);body+=line
        if current:chunks.append((current,body))
        results=[]
        # Compute first; a failed part never marks a source as archived.
        try:
            for items,text in chunks:
                summary=await self.summarize(text)
                if not summary or not summary.strip():return []
                results.append((items,summary.strip()))
        except Exception:return []
        grouped={}
        for items,summary in results:
            key=tuple(i['id'] for i in items)
            if key in grouped:grouped[key][1].append(summary)
            else:grouped[key]=[items,[summary]]
        ids=[]
        for items,parts in grouped.values():
            text='\n'.join(parts)
            title_match=re.search(r'\[话题\]\s*([^\n]+)',text)
            title=title_match[1][:80] if title_match else '群聊话题'
            sid=self.store.commit_summary(gid,items,title,text,generation=generation)
            if sid:ids.append(sid)
        self.rebuild_daily(gid)
        return ids

    def rebuild_daily(self,gid):
        with self.store.transaction() as c:
            version=tuple(c.execute('SELECT count(*),max(id) FROM messages WHERE gid=?',(gid,)).fetchone())
            version+=tuple(c.execute("SELECT count(*),max(ts),sum(case when state='active' then 1 else 0 end) FROM memories WHERE gid=?",(gid,)).fetchone())
            version+=tuple(c.execute("SELECT count(*),max(ts) FROM summaries WHERE gid=? AND state='active'",(gid,)).fetchone())
            version+=tuple(c.execute('SELECT generation FROM groups WHERE gid=?',(gid,)).fetchone() or ())
        if self._daily_versions.get(gid)==version:return
        summaries=self.store.summaries(gid,limit=10000)
        days={}
        for r in summaries:
            source_ids=json_ids(r['source_ids'])
            with self.store.transaction() as c:
                dates={datetime.fromtimestamp(row[0],TZ).date().isoformat() for mid in source_ids for row in c.execute('SELECT ts FROM messages WHERE gid=? AND id=?',(gid,mid))}
            if not dates:dates={datetime.fromtimestamp(r['start_ts'],TZ).date().isoformat()}
            for day in dates:days.setdefault(day,[]).append((r['id'],r['title']))
        with self.store.transaction() as c:
            # Even a quiet day with fewer than 20 messages has a discoverable source index.
            raw=c.execute("SELECT date(ts,'unixepoch','+8 hours') AS day,min(id),max(id),count(*) FROM messages WHERE gid=? GROUP BY day",(gid,)).fetchall()
            for row in raw:days.setdefault(row[0],[]).append(('',f'原始聊天 {row[1]}—{row[2]}，共{row[3]}条，可按日期回查'))
            for row in c.execute("SELECT id,text,ts,state,event_time FROM memories WHERE gid=? AND explicit=1 AND state<>'deleted'",(gid,)):
                day=datetime.fromtimestamp(row['ts'],TZ).date().isoformat()
                days.setdefault(day,[]).append((row['id'],f"{row['text'][:80]}（{row['state']}；事件日期{row['event_time'] or '未明确'}）"))
            for day,rows in days.items():
                text='\n'.join(f"[{rid}] {title}" if rid else title for rid,title in rows)
                c.execute('INSERT OR REPLACE INTO daily_indexes VALUES(?,?,?,?,?)',(gid,day,text,encode([rid for rid,_ in rows if rid]),time.time()))
        self._daily_versions[gid]=version


def json_ids(text):
    import json
    return json.loads(text or '[]')
