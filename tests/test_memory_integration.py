import tempfile
import types
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
from test_group_gate import Event,plugin_with_fake_astrbot


class MemoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_during_identity_lookup_cannot_reintroduce_old_request(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            Plugin=plugin_with_fake_astrbot(tmp);bot=Plugin(types.SimpleNamespace())
            bot.cfg.record_store_enable=bot.cfg.record_store_read_enable=bot.cfg.identity_enable=True
            bot._init_memory_store(Path(tmp))
            entered=asyncio.Event();release=asyncio.Event()
            async def call(action,**kwargs):
                if action=='get_identity_directory':entered.set();await release.wait()
                return None
            event=Event('alice','记住：旧请求',at=True);event.bot=types.SimpleNamespace(call_action=call)
            task=asyncio.create_task(bot.on_group_message(event));await entered.wait()
            bot._cancel_group_activity('group-1');release.set();await task
            self.assertEqual(bot.records.message_count('group-1'),0)
            self.assertFalse(event.get_extra('ga_admitted',False))

    async def test_l20_is_attached_below_the_corresponding_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin=plugin_with_fake_astrbot(tmp);bot=Plugin(types.SimpleNamespace())
            bot.cfg.record_store_enable=bot.cfg.record_store_read_enable=bot.cfg.l20_inject_enable=True
            bot._init_memory_store(Path(tmp))
            bot.chatlog.push('g',{'user':'甲','text':'看这张图','ts':1})
            reply=bot.chatlog.push('g',{'user':'本鱼','text':'图里有一只猫','ts':2})
            bot.tool_trace.record('g','r','look_image',{'image_id':12},{'ok':True,'data':'bytes'},reply_id=reply)
            bot.chatlog.push('g',{'user':'甲','text':'什么颜色','ts':3})
            context=bot._render_chatlog('g')
            self.assertIn('L20',context)
            self.assertLess(context.index('图里有一只猫'),context.index('L20'))

    async def test_explicit_save_then_alias_recall_in_new_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            Plugin=plugin_with_fake_astrbot(tmp)
            bot=Plugin(types.SimpleNamespace())
            bot.cfg.record_store_enable=bot.cfg.record_store_read_enable=True
            bot.cfg.identity_enable=bot.cfg.explicit_memory_enable=bot.cfg.memory_prelookup_enable=True
            bot.cfg.l20_record_enable=bot.cfg.l20_inject_enable=True
            bot.cfg.merge_enable=bot.cfg.preflight_enable=bot.cfg.kb_enable=False
            bot._init_memory_store(Path(tmp))
            async def call(action,**params):
                if action=='get_identity_directory':return {'account':'bot','group_wxid':'g@chatroom','members':[{'wxid':'wxid_a','user_id':'alice','nickname':'陈sir','aliases':[{'name':'Amy','source':'reference'}]}]}
                if action=='get_message_identity':return {'sender_wxid':'wxid_a','account':'bot','source_key':'src:'+str(params['message_id'])}
                return {}
            event=Event('alice','记住：下周三交初稿',at=True)
            event.message_obj.message_id=1;event.bot=types.SimpleNamespace(call_action=call)
            await bot.on_group_message(event)
            req=types.SimpleNamespace(contexts=[],image_urls=[],prompt=event.message_str,extra_user_content_parts=[],system_prompt='persona')
            await bot.inject_memory(event,req)
            self.assertEqual(len(bot.records.search_memories('group-1',explicit=True)),1)
            self.assertIn('已保存',event.get_extra('ga_memory_receipt'))
            next_event=Event('alice','Amy之前让你记住什么',at=True)
            next_event.message_obj.message_id=2;next_event.bot=event.bot
            await bot.on_group_message(next_event)
            next_req=types.SimpleNamespace(contexts=[],image_urls=[],prompt=next_event.message_str,extra_user_content_parts=[],system_prompt='persona')
            await bot.inject_memory(next_event,next_req)
            self.assertIn('交初稿',next_event.get_extra('ga_memory_lookup')['text'])
            self.assertIn('回答前记忆查询',str(next_req.extra_user_content_parts))


if __name__=='__main__':unittest.main()
