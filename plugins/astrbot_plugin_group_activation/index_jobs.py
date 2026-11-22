"""Single-consumer outbox; deterministic document names make retries idempotent."""
import asyncio


class IndexJobs:
    def __init__(self,store,kbs,allowed=None):self.store=store;self.kbs=kbs;self.allowed=allowed or (lambda gid:True)

    def _source(self,job):
        if self.store.generation(job['gid'])!=job['generation']:return None
        with self.store.transaction() as c:
            table='summaries' if job['object_id'].startswith('S') else 'memories'
            row=c.execute(f"SELECT * FROM {table} WHERE id=? AND gid=? AND state='active'",(job['object_id'],job['gid'])).fetchone()
            return dict(row) if row else None

    async def run_once(self):
        completed=0
        attempted=0
        for job in self.store.pending_jobs(limit=1000):
            if not self.allowed(job['gid']):continue
            attempted+=1
            if attempted>5:break
            source=self._source(job)
            if not source:continue
            helper=None;docid=None
            try:
                helper=await asyncio.wait_for(self.kbs._helper(job['gid']),timeout=10)
                if helper is None:raise RuntimeError('知识库未就绪')
                filename='webot-'+source['id']+'.txt'
                docs=await asyncio.wait_for(helper.list_documents(search=filename,limit=20),timeout=10)
                existing=next((d for d in docs if getattr(d,'doc_name','')==filename),None)
                if existing:docid=existing.doc_id
                else:
                    text=f"[webot-memory:{source['id']}]\n"+source.get('title','')+'\n'+source['text']
                    # Dedicated path: full summaries are chunked, never clipped to kb_max_chars.
                    chunks=[text[i:i+1800] for i in range(0,len(text),1800)]
                    chunks=[chunk if i==0 else f"[webot-memory:{source['id']}]\n"+chunk for i,chunk in enumerate(chunks)]
                    doc=await asyncio.wait_for(helper.upload_document(file_name=filename,file_content=None,file_type='txt',pre_chunked_text=chunks),timeout=45)
                    docid=doc.doc_id
                if not self._source(job):
                    if docid:await helper.delete_document(docid)
                    continue
                self.store.finish_job(job['id'],external_id=docid)
                completed+=1
            except Exception as e:self.store.finish_job(job['id'],error=type(e).__name__)
        return completed
