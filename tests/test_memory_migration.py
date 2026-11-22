import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from contextlib import closing


class MigrationTests(unittest.TestCase):
    def test_new_managed_index_is_not_reimported_as_legacy_after_restart(self):
        from plugins.astrbot_plugin_group_activation.migration import migrate
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data/plugin_data/p';root.mkdir(parents=True)
            kb=root.parent.parent/'knowledge_base';(kb/'k').mkdir(parents=True)
            with closing(sqlite3.connect(kb/'kb.db')) as c, c:
                c.execute('CREATE TABLE knowledge_bases(kb_id,kb_name)')
                c.execute('INSERT INTO knowledge_bases VALUES(?,?)',('k','群聊记忆-g'))
            with closing(sqlite3.connect(kb/'k/doc.db')) as c, c:
                c.execute('CREATE TABLE documents(doc_id,text,metadata)')
                c.executemany('INSERT INTO documents VALUES(?,?,?)',[('old','原有知识','{}'),('new','[webot-memory:M123]已取消的事项','{}')])
            self.assertEqual(migrate(root,dry_run=True)['legacy_chunks'],1)
            migrate(root)
            self.assertEqual(len(RecordStore(root/'memory_store.sqlite3').search_memories('g')),1)

    def test_dry_run_is_read_only_and_apply_is_idempotent(self):
        from plugins.astrbot_plugin_group_activation.migration import migrate
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'chatlog').mkdir()
            (root/'chatlog/g.jsonl').write_text(json.dumps({'user':'甲','text':'原话','ts':10})+'\n',encoding='utf-8')
            (root/'long_term_memory.json').write_text(json.dumps({'g':{'members':{'甲':{'facts':[{'text':'喜欢红茶','first_ts':'2026-11-01T10:00:00','last_ts':'2026-11-02T10:00:00','hits':2}]}}}}),encoding='utf-8')
            report=migrate(root,dry_run=True)
            self.assertEqual(report['messages'],1)
            self.assertFalse((root/'memory_store.sqlite3').exists())
            migrate(root)
            migrate(root)
            s=RecordStore(root/'memory_store.sqlite3')
            self.assertEqual(s.message_count('g'),1)
            self.assertEqual(len(s.search_memories('g')),1)
            self.assertEqual(s.search_memories('g')[0]['pid'],'legacy:甲')


if __name__=='__main__':unittest.main()
