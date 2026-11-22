"""Program-controlled recall with explicit identity, date and coverage states."""
from datetime import datetime,timedelta
import re
import time
from .memory_service import TZ


def is_recall(text):
    return bool(re.search(r'记得|记住过|记过|让你记|叫你记|让你保存|之前|以前|上次|昨天|前天|曾经|日记|群规|约定|最后定|是什么人|是谁|什么特点|喜欢|口味|忌口|职业|生日|擅长|不吃|看过.*图',text or ''))


class MemoryRetrieval:
    def __init__(self,store,identity):self.store=store;self.identity=identity

    def lookup(self,gid,text,*,person_id=None,now=None,limit=8):
        try:return self._lookup(gid,text,person_id=person_id,now=now,limit=limit)
        except Exception as exc:
            return {'status':'unavailable','text':'本地记忆查询失败：'+type(exc).__name__,'evidence':[],
                    'coverage':'查询未完成，不能断言没有这件事。'}

    def _lookup(self,gid,text,*,person_id=None,now=None,limit=8):
        now=time.time() if now is None else now
        start=end=None
        current=datetime.fromtimestamp(now,TZ)
        for word,offset in (('前天',-2),('昨天',-1),('今天',0)):
            if word in text:
                beginning=(current+timedelta(days=offset)).replace(hour=0,minute=0,second=0,microsecond=0)
                start=beginning.timestamp();end=(beginning+timedelta(days=1)).timestamp();break
        if start is None:
            m=re.search(r'(20\d\d)[年/-](\d{1,2})[月/-](\d{1,2})',text)
            if m:
                try:
                    beginning=current.replace(year=int(m[1]),month=int(m[2]),day=int(m[3]),hour=0,minute=0,second=0,microsecond=0)
                    start=beginning.timestamp();end=(beginning+timedelta(days=1)).timestamp()
                except ValueError:return {'status':'ambiguous','text':'日期无效，需要澄清。','evidence':[]}
        names=[]
        with self.store.transaction() as c:
            aliases=[r[0] for r in c.execute('SELECT DISTINCT name FROM aliases WHERE gid=? ORDER BY length(name) DESC',(gid,))]
        for name in aliases:
            if len(name)==1 and not re.search(r'(?:^|@|「|关于)'+re.escape(name)+r'(?=是谁|的|喜欢|不吃|职业|生日|擅长|$|」|\s)',text):continue
            pattern=re.escape(name)
            if re.fullmatch(r'[\w .-]+',name,re.ASCII):pattern=r'(?<![A-Za-z0-9_])'+pattern+r'(?![A-Za-z0-9_])'
            if re.search(pattern,text,re.I) and not any(name in n for n in names):names.append(name)
        chosen=None
        for name in names:
            resolved=self.identity.resolve(gid,name)
            if resolved['status']=='ambiguous':return {'status':'ambiguous','text':'本群有多人使用该名字，需要明确对象。','evidence':[]}
            if resolved['status']=='found':
                candidate=resolved['people'][0]
                if chosen and chosen['pid']!=candidate['pid']:return {'status':'ambiguous','text':'涉及多位成员，请明确要查哪一位。','evidence':[]}
                chosen=candidate
        pid=chosen['pid'] if chosen else person_id if '我' in text else None
        if chosen is None and '我' in text and person_id is None:
            return {'status':'ambiguous','text':'暂未确认你的微信身份，不能把其他成员的记录当成你的。请明确身份或引用原消息。','evidence':[]}
        explicit=True if re.search(r'让你记|叫你记|让我记|让你保存|记住什么|保存过',text) else None
        kind='profile_fact' if re.search(r'是谁|是什么人|什么特点|喜欢|口味|忌口|职业|生日|擅长|不吃',text) else None
        query=text
        for name in names:query=query.replace(name,'')
        query=re.sub(r'那件事|这件事|具体内容|是什么|交代|提到|告诉|聊过|聊了|记下|记着|有没有|情况|内容',' ',query)
        query=re.sub(r'20\d\d[年/-]\d{1,2}[月/-]\d{1,2}|[\[\]At:\d]+|昨天|今天|前天|之前|以前|上次|曾经|请|你|我|让|叫|帮|记住|记得|记过|保存|什么|哪些|的|吗|过|说|事情|这件事|到底|还|在|了',' ',query)
        words=[w for w in re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+',query) if len(w)>=2]
        expanded=[]
        for word in words:
            expanded.append(word)
            if re.fullmatch(r'[\u4e00-\u9fff]{3,12}',word):expanded.extend(word[i:i+2] for i in range(len(word)-1))
        overview=bool(re.search(r'聊.*什么|聊.*哪些|日记|发生.*什么|说过什么|说了什么|交代.*什么',text))
        terms=[] if kind or overview else list(dict.fromkeys(expanded))[:20]
        try:
            records=self.store.search_memories(gid,terms,person_id=pid,kind=kind,explicit=explicit,start=start,end=end,limit=limit,include_inactive=bool(explicit))
            sources=self.store.search_messages(gid,terms,person_id=pid,start=start,end=end,limit=max(100,limit*8)) if not kind else []
            if terms:sources.sort(key=lambda r:(sum(len(t) for t in terms if t.casefold() in r['text'].casefold()),r['ts']),reverse=True)
            sources=sources[:limit]
            summaries=self.store.summaries(gid,start,end,limit) if pid is None and not kind else []
            if terms:summaries=[s for s in summaries if any(t.casefold() in (s['title']+s['text']).casefold() for t in terms)]
            lines=[]
            if pid is None and re.search(r'聊.*什么|聊.*哪些|日记|发生.*什么',text):
                with self.store.transaction() as c:
                    if start is not None:
                        day=datetime.fromtimestamp(start,TZ).date().isoformat()
                        daily=c.execute('SELECT day,text FROM daily_indexes WHERE gid=? AND day=?',(gid,day)).fetchall()
                    else:daily=c.execute('SELECT day,text FROM daily_indexes WHERE gid=? ORDER BY day DESC LIMIT 7',(gid,)).fetchall()
                lines.extend(f"[每日索引 {r['day']}] {r['text']}" for r in daily)
            for r in records:
                lines.append(f"[{r['id']} | {datetime.fromtimestamp(r['ts'],TZ).isoformat()} | {r['kind']} | 状态:{r['state']}] {r['text']}"+(f"；事件日期 {r['event_time']}" if r['event_time'] else ''))
                if r['state']=='superseded':
                    with self.store.transaction() as c:
                        replacement=c.execute("SELECT id,text,state FROM memories WHERE gid=? AND replaces=? ORDER BY ts DESC LIMIT 1",(gid,r['id'])).fetchone()
                    if replacement:lines.append(f"  后续修改为 [{replacement['id']},状态:{replacement['state']}] {replacement['text']}")
            # For explicit saves, ordinary raw chatter does not prove that saving succeeded.
            if not explicit:
                for r in summaries:lines.append(f"[{r['id']} | 总结，需核实细节] {r['title']}：{r['text']}")
            if not explicit and not kind:
                for r in sources:lines.append(f"[原文 {r['id']} | {datetime.fromtimestamp(r['ts'],TZ).isoformat()} | {r['user']}] {r['text'][:600]}")
            evidence=(records+([] if explicit else summaries+sources))[:limit]
            return {'status':'found' if lines else 'not_found','text':'\n'.join(lines[:limit])[:4000],
                    'evidence':evidence,'person':chosen,'range':{'start':start,'end':end},
                    'coverage':'仅当前保留来源；未命中不证明从未发生。'}
        except Exception as e:return {'status':'unavailable','text':'本地记忆查询失败：'+type(e).__name__,'evidence':[]}
