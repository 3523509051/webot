"""Exercise real bridge reply serialization, using fake UI and websocket only."""
import asyncio
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock


BRIDGE = Path(__file__).resolve().parents[1] / 'bridge'
sys.path.insert(0, str(BRIDGE))
from onebot11 import OneBotClient, ActionFailed
from send_result import SendResult
from wechat_client import WeChatClient
sys.path.remove(str(BRIDGE))


class SendOutcomeTests(unittest.TestCase):
    def test_successful_direct_send_is_confirmed_without_switching_chat(self):
        client = WeChatClient.__new__(WeChatClient)
        client.verify_send = True
        client._names = ['测试群']
        uia = types.SimpleNamespace(current_chat=lambda: '测试群', send_text=Mock(return_value=True))
        mark = {'seq': 7}
        verify = Mock(return_value=True)
        client.gui = types.SimpleNamespace(_get_uia=lambda: uia, send_msg=Mock(),
                                          _send_watermark=lambda name: mark, _verify_sent=verify)
        self.assertEqual(client.send_text('测试群', 'hello').state, 'sent')
        verify.assert_called_once_with('hello', '测试群', mode='exact', after=mark)
        client.gui.send_msg.assert_not_called()

    def test_gui_failure_is_not_reported_as_success(self):
        client = WeChatClient.__new__(WeChatClient)
        client.verify_send = True
        client.gui = types.SimpleNamespace(_get_uia=lambda: None,
            send_msg=Mock(return_value={'status': '失败', 'message': '微信窗口不可见'}))
        result = client.send_text('测试群', 'hello')
        self.assertFalse(result)
        self.assertEqual(result.state, 'failed')
        client.gui.send_msg.assert_called_once()

    def test_database_uncertainty_and_uia_uncertainty_do_not_resend(self):
        client = WeChatClient.__new__(WeChatClient)
        client.verify_send = True
        client.gui = types.SimpleNamespace(_get_uia=lambda: None,
            send_msg=Mock(return_value={'status': '失败', 'message': '消息已操作发送，但数据库未确认'}))
        self.assertEqual(client.send_text('测试群', 'hello').state, 'unknown')
        client.gui.send_msg.assert_called_once()
        client.verify_send = False
        client._names = ['测试群']
        uia = types.SimpleNamespace(current_chat=lambda: '测试群', send_text=lambda text: False)
        client.gui = types.SimpleNamespace(_get_uia=lambda: uia, send_msg=Mock())
        self.assertEqual(client.send_text('测试群', 'hello').state, 'unknown')
        client.gui.send_msg.assert_not_called()
        client._uia_send_at = Mock(return_value=SendResult('unknown', '回车结果不确定'))
        client.send_text = Mock()
        self.assertEqual(client.send_at('测试群', 'alice', '', 'hello').state, 'unknown')
        client.send_text.assert_not_called()

    def test_protocol_propagates_failure_unknown_and_echo_without_retry(self):
        async def scenario():
            for outcome in ('sent', 'failed', 'unknown', 'unsupported'):
                calls, replies = [], []

                async def handler(action, params):
                    calls.append(action)
                    if outcome in ('failed', 'unknown'):
                        raise ActionFailed('test', delivery_state=outcome,
                                           retcode=1201 if outcome == 'unknown' else 1200)
                    return {'message_id': 42} if outcome == 'sent' else None

                client = OneBotClient('ws://unused', 10001, action_handler=handler)

                async def send(value):
                    replies.append(value)

                client._send_json = send
                await client._on_raw(json.dumps({'action': 'send_group_msg', 'params': {}, 'echo': 'ticket'}))
                self.assertEqual(len(calls), 1)
                reply = replies[0]
                self.assertEqual(reply['echo'], 'ticket')
                self.assertEqual(reply['status'], 'ok' if outcome == 'sent' else 'failed')
                if outcome == 'sent':
                    self.assertEqual(reply['data']['message_id'], 42)
                else:
                    self.assertNotEqual(reply['retcode'], 0)
                    self.assertFalse(reply['data']['retry_safe'])
                if outcome == 'unknown':
                    self.assertEqual(reply['data']['delivery_state'], 'unknown')

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
