"""Idempotent legacy imports; unresolved names stay unresolved."""
from datetime import datetime
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from .memory_service import TZ
from .record_store import RecordStore,encode


def migrate(data_dir,*,dry_run=False,store=None):
    root=Path(data_dir)
    report={'messages':0,'profiles':0,'legacy_chunks':0,'unresolved_names':[],'dry_run':dry_run}
    if not dry_run:store=store or RecordStore(root/'memory_store.sqlite3')
    for path in sorted((root/'chatlog').glob('*.jsonl')):
        gid=path.stem;marker='legacy-jsonl:'+gid
        done=False if dry_run else store.migration_done(marker)
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip():continue
            item=json.loads(line)
            report['messages']+=1
            if not dry_run and not done:
                key='legacy:'+hashlib.sha256(encode([gid,item.get('ts'),item.get('user'),item.get('text')]).encode()).hexdigest()
                store.append_message(gid,{**item,'source_key':item.get('source_key') or key,'legacy_unresolved':True})
        if not dry_run and not done:store.mark_migration(marker,report)
    l3=root/'long_term_memory.json'
    if l3.exists():
        data=json.loads(l3.read_text(encoding='utf-8-sig'))
        for gid,group in data.items():
            marker='legacy-l3:'+gid
            done=False if dry_run else store.migration_done(marker)
            for name,member in group.get('members',{}).items():
                report['unresolved_names'].append({'group':gid,'name':name})
                for fact in member.get('facts',[]):
                    report['profiles']+=1
                    if dry_run or done:continue
                    stamp=fact.get('last_ts') or member.get('updated_at')
                    try:ts=datetime.fromisoformat(stamp).replace(tzinfo=TZ).timestamp()
                    except (TypeError,ValueError):ts=0
                    store.save_memory(gid,fact.get('text',''),kind='profile_fact',person_id='legacy:'+name,
                        metadata={**fact,'legacy_name':name,'legacy_source':'long_term_memory.json'},recorded_at=ts)
            if not dry_run and not done:store.mark_migration(marker,report)
    kbroot=root.parent.parent/'knowledge_base'
    kb=kbroot/'kb.db'
    if kb.is_file():
        with closing(sqlite3.connect(kb.as_uri()+'?mode=ro',uri=True)) as c:
            libraries=c.execute('SELECT kb_id,kb_name FROM knowledge_bases').fetchall()
        for kid,name in libraries:
            if not name.startswith('群聊记忆-'):continue
            gid=name[len('群聊记忆-'):]
            path=kbroot/kid/'doc.db'
            if not path.exists():continue
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
                chunks=c.execute('SELECT doc_id,text,metadata FROM documents').fetchall()
            for docid,text,metadata in chunks:
                if '[webot-memory:' in text:continue  # our own index is not a legacy source
                report['legacy_chunks']+=1
                marker='legacy-kb:'+kid+':'+docid
                if dry_run or store.migration_done(marker):continue
                saved=store.save_memory(gid,text,kind='legacy',metadata={'legacy_doc_id':docid,'legacy_kb_id':kid,'source_metadata':metadata})
                with store.transaction() as c:c.execute("UPDATE index_jobs SET state='done' WHERE object_id=?",(saved['id'],))
                store.mark_migration(marker,{'id':saved['id']})
    return report
