"""Explicit saves are committed independently of the main model's tool choice."""
from datetime import datetime,timedelta,timezone
import re
import time

TZ=timezone(timedelta(hours=8))
_SAVE=re.compile(r'^(?:\[At:\d+\]\s*)*(?:(?:我想让你|我希望你|能不能帮我|请|你|帮我|麻烦你|给我|一定要|要)\s*)*(?:记住|记下|记一下|保存一下|保存这件事|记到日记里)[：:，,\s]*(.*)$',re.S)
_CHANGE=re.compile(r'^(?:请|帮我)?(?:把)?(?:刚才|之前|上次)?(?:记住的|记下的|那条记录|那件事)[，,\s]*(?:改成|改为|修改为)[：:，,\s]*(.+)$',re.S)
_CANCEL=re.compile(r'^(?:请|帮我)?取消(?:刚才|之前|上次)?(?:记住的|记下的|保存的|那条记录)[：:，,\s]*(.*)$',re.S)


def save_intent(text):
    text=(text or '').strip()
    if re.search(r'^(?:不要|不用|别|无需|不必)|(?:记住|记下).*(?:吗|么)[？?]?$|^(?:他说|她说|有人说)',text):return None
    change=_CHANGE.match(text)
    if change:return {'action':'replace','content':change.group(1).strip()}
    cancel=_CANCEL.match(text)
    if cancel:return {'action':'cancel','content':cancel.group(1).strip()}
    match=_SAVE.match(text)
    if match:return {'action':'save','content':match.group(1).strip()}
    return None


def resolve_event_time(text,now=None):
    date=datetime.fromtimestamp(now if now is not None else time.time(),TZ).date()
    m=re.search(r'(20\d\d)[年/-](\d{1,2})[月/-](\d{1,2})',text)
    if m:
        try:return date.replace(year=int(m[1]),month=int(m[2]),day=int(m[3])).isoformat()
        except ValueError:return ''
    for word,days in (('大后天',3),('后天',2),('明天',1),('今天',0),('昨天',-1)):
        if word in text:return (date+timedelta(days=days)).isoformat()
    m=re.search(r'(下周|本周|这周|周|星期)([一二三四五六日天])',text)
    if m:
        weekday='一二三四五六日'.find(m[2].replace('天','日'))
        delta=weekday-date.weekday()
        if m[1]=='下周':delta+=7
        elif m[1] in ('周','星期') and delta<0:delta+=7
        return (date+timedelta(days=delta)).isoformat()
    return ''


class MemoryService:
    def __init__(self,store,identity=None):self.store=store;self.identity=identity

    def capture(self,gid,pid,text,source_ids=(),now=None):
        try:return self._capture(gid,pid,text,source_ids,now)
        except Exception as e:return [{'status':'failed','text':'可靠保存未完成','error':type(e).__name__}]

    def _capture(self,gid,pid,text,source_ids=(),now=None):
        intent=save_intent(text)
        if not intent:return []
        content=intent['content']
        if intent['action']=='save' and re.search(r'改成|改为|修改为',content):
            return [{'status':'needs_clarification','text':'请明确要修改的原事项，例如“把之前记住的改成周四交初稿”，避免旧安排与新安排同时生效。'}]
        if intent['action']!='cancel' and (not content or re.fullmatch(r'(这个|那个|这件事|刚才那个|上面那个)[。！!]*',content)):
            return [{'status':'needs_clarification','text':'请明确要保存的内容，或引用那条消息。'}]
        if pid is None:
            return [{'status':'needs_clarification','text':'暂未确认你的微信身份，未将内容归到任何同名成员。请稍后再试。'}]
        replaces=None
        if intent['action'] in ('replace','cancel'):
            candidates=self.store.search_memories(gid,person_id=pid,explicit=True,limit=20)
            topic=lambda t: re.sub(r'(?:下周|本周|这周|周|星期)[一二三四五六日天]|\d+|[年月日号\s，。,:：]','',t)
            target=topic(content)
            matches=[r for r in candidates if target and (target in topic(r['text']) or topic(r['text']) in target)]
            if intent['action']=='cancel' and not content and len(candidates)==1:matches=candidates
            if len(matches)!=1:
                return [{'status':'needs_clarification','text':'无法唯一确定要修改哪条记录，请带上原事项内容。'}]
            replaces=matches[0]['id']
            if intent['action']=='cancel':
                try:return [self.store.cancel_memory(gid,replaces)]
                except Exception:return [{'status':'failed','text':'取消保存失败，原记录状态未确认改变。'}]
        result=[]
        for part in re.split(r'[；;]\s*(?:另外[，,]?\s*)?|\n\s*\d+[.、]\s*',content):
            if not part.strip():continue
            try:
                subjects=[]
                if self.identity is not None:
                    with self.store.transaction() as c:
                        names=[r[0] for r in c.execute('SELECT DISTINCT name FROM aliases WHERE gid=? ORDER BY length(name) DESC',(gid,))]
                    matched_names=[]
                    for name in names:
                        pattern=re.escape(name)
                        if re.fullmatch(r'[A-Za-z0-9_]+',name):pattern=r'(?<![A-Za-z0-9_])'+pattern+r'(?![A-Za-z0-9_])'
                        if re.search(pattern,part,re.I) and not any(name in n for n in matched_names):
                            match=self.identity.resolve(gid,name)
                            if match['status']=='found':subjects.append(match['people'][0]['pid']);matched_names.append(name)
                if not subjects and re.search(r'我|本人',part):subjects=[pid]
                category='profile_fact' if subjects and re.search(r'喜欢|爱吃|不吃|忌口|职业|专业|生日|擅长|过敏|讨厌',part) else 'event'
                result.append(self.store.save_memory(gid,part.strip(),kind='explicit',person_id=pid,explicit=True,
                    original=text,source_ids=source_ids,event_time=resolve_event_time(part,now),replaces=replaces,
                    recorded_at=now,metadata={'subject_ids':list(set(subjects)),'category':category}))
            except Exception as e:
                result.append({'status':'failed','text':part,'error':type(e).__name__})
        return result

    @staticmethod
    def receipt(results):
        lines=[]
        labels={'saved':'已记住','saved_index_pending':'已保存原文，语义索引待完成',
                'already_exists':'此前已保存','needs_clarification':'需要明确内容','failed':'保存失败，未确认记住','cancelled':'已取消保存'}
        for r in results:
            lines.append(labels.get(r['status'],r['status'])+'：'+r.get('text','')+('〔'+r['id']+'〕' if r.get('id') else ''))
        return '\n'.join(lines)
