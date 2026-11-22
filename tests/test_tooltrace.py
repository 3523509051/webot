import tempfile
import unittest
from pathlib import Path


class ToolTraceTests(unittest.TestCase):
    def test_file_trace_keeps_partial_read_boundary_before_content(self):
        from plugins.astrbot_plugin_group_activation.tooltrace import ToolTrace
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            trace=ToolTrace(RecordStore(Path(tmp)/'x.db'))
            trace.record('g','r','read_file',{'file_id':'f1','max_chars':100},'文件 #f1 report.md\n'+'x'*2000+'\n内容已截断')
            rendered=trace.render('g')
            self.assertIn('截断=是',rendered)
            self.assertIn('100',rendered)

    def test_image_read_and_list_are_distinct_and_survive_restart(self):
        from plugins.astrbot_plugin_group_activation.tooltrace import ToolTrace
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            store=RecordStore(Path(tmp)/'x.db');trace=ToolTrace(store)
            trace.record('g','r','list_images',{'limit':20},'本群缓存图片 #12')
            trace.record('g','r','look_image',{'image_id':12},{'ok':True,'id':12,'data':'SECRET_BASE64'})
            trace.record('g','r','read_file',{'file_id':'f1'},'文件读取失败：找不到')
            rendered=ToolTrace(RecordStore(store.path)).render('g')
            self.assertIn('图片 #12',rendered)
            self.assertIn('failed',rendered)
            self.assertIn('目录',rendered)
            self.assertNotIn('SECRET_BASE64',rendered)
            self.assertEqual(trace.render('other'),'')
            store.reset_context('g')
            self.assertEqual(trace.render('g'),'')

    def test_invalid_image_arguments_are_not_recorded_as_viewed(self):
        from plugins.astrbot_plugin_group_activation.tooltrace import ToolTrace
        from plugins.astrbot_plugin_group_activation.record_store import RecordStore
        with tempfile.TemporaryDirectory() as tmp:
            store=RecordStore(Path(tmp)/'x.db');trace=ToolTrace(store)
            trace.record('g','r','look_image',{'image_id':'abc'},'image_id 必须是数字编号。')
            self.assertNotIn('已返回图片数据',trace.render('g'))


if __name__=='__main__':unittest.main()
