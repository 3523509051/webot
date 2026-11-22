"""Compact receipts of tool use; never persist image payloads or credentials."""
import json
import re
import time
import uuid


def result_text(result):
    if isinstance(result,str):return result
    if isinstance(result,dict):
        safe={k:v for k,v in result.items() if k in ('ok','error','id','name','sender','create_time','mime','status','count','truncated','source','results')}
        return json.dumps(safe,ensure_ascii=False,default=str)
    parts=getattr(result,'content',[]) or []
    return ' '.join(str(getattr(p,'text','')) for p in parts if getattr(p,'type','')=='text')


class ToolTrace:
    def __init__(self,store):self.store=store

    def record(self,gid,request_id,tool,args,result,*,status=None,reply_id=None):
        text=result_text(result)
        if status is None:
            failed=(isinstance(result,dict) and result.get('ok') is False) or getattr(result,'isError',False)
            failed=failed or bool(re.search(r'失败|不存在|不可用|无法|没有可读取|超时|取消|已过期',text))
            status='failed' if failed else 'not_found' if re.search(r'没有匹配|没找到|没有和|没有.*条目',text) else 'returned'
        args=args or {}
        if tool=='look_image' and status=='returned':
            image_data=bool(result.get('data')) if isinstance(result,dict) else any(getattr(p,'type','')=='image' for p in (getattr(result,'content',[]) or []))
            if not image_data:status='already_provided' if '本轮提供过' in text else 'failed'
        obj=str(args.get('image_id') or args.get('file_id') or args.get('sticker_id') or args.get('url') or '')
        if tool=='look_image':
            prefix='图片 #'+obj+'；'+('已返回图片数据' if status=='returned' else '本轮已提供，无需重复读取' if status=='already_provided' else '未确认读取')
        elif tool.startswith('list_'):prefix='目录查询，不代表读取内容'
        elif tool=='auto_image':prefix='自动附图；'+obj
        elif tool=='read_file':prefix=f"文件 {obj}；读取前{args.get('max_chars',12000)}字符；截断="+('是' if '已截断' in text or '截断=是' in text else '未报告截断')
        else:prefix=tool
        # Only small identifiers/queries, not arbitrary payload parameters.
        hints='；'.join(str(args[k])[:100] for k in ('query','city','names','file_id') if args.get(k))
        safe=re.sub(r'base64[^\s]*|(?:aeskey|filekey|token|api_key)\s*[=:]\s*\S+','[已略]',text,flags=re.I)
        summary=(prefix+'；'+hints+'；'+safe)[:400]
        eid='T'+uuid.uuid4().hex
        with self.store.transaction() as c:
            self.store._group(c,gid)
            generation=c.execute('SELECT generation FROM groups WHERE gid=?',(gid,)).fetchone()[0]
            c.execute('INSERT INTO tool_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (eid,gid,request_id,tool,obj,status,summary,time.time(),reply_id,'{}',generation))
        return eid

    def render(self,gid,request_ids=None,budget=1500,orphan_only=False,reply_id=None):
        with self.store.transaction() as c:
            generation=c.execute('SELECT generation FROM groups WHERE gid=?',(gid,)).fetchone()
            if not generation:return ''
            rows=c.execute('SELECT * FROM tool_events WHERE gid=? AND generation=? ORDER BY ts DESC LIMIT 100',(gid,generation[0])).fetchall()
            if request_ids is None:
                request_ids=list(dict.fromkeys(r['request_id'] for r in rows))[:6]
            lines=[];size=0
            for r in reversed(rows):
                if r['request_id'] not in request_ids:continue
                if orphan_only and r['reply_id'] is not None:continue
                if reply_id is not None and r['reply_id']!=reply_id:continue
                line=f"【L20 {r['id']} | 请求 {r['request_id']} | {r['status']}】{r['summary'][:200]}"
                if size+len(line)>budget:continue
                lines.append(line);size+=len(line)
            return '\n'.join(lines)
