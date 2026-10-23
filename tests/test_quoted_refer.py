# -*- coding: utf-8 -*-
"""引用**表情包**（refermsg 的 content 是消息 ID 而非图片 XML）必须能解析出 md5。

2026-10-16 实况：群里「引用一张表情包 + @机器人 这是什么」，桥解析不出图，
还把 ID 串 `wxid_xxx:1792116875436:1:fb0db…::0` 当正文喂给了模型。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BRIDGE))
_spec = importlib.util.spec_from_file_location("webot_bridge_main", BRIDGE / "main.py")
bridge_main = importlib.util.module_from_spec(_spec)
sys.modules["webot_bridge_main"] = bridge_main
_spec.loader.exec_module(bridge_main)
sys.path.pop(0)

STICKER_REF = (
    '<msg><appmsg><title>这是什么</title><type>57</type>'
    "<refermsg><displayname>小泽布尔</displayname>"
    "<content>wxid_t2eu8ncgfdh522:1792116875436:1:"
    "fb0dbcea25affabed88336bb23f520d1::0</content>"
    "<createtime>1792116879</createtime></refermsg></appmsg></msg>"
)
TEXT_REF = (
    '<msg><appmsg><title>哈哈</title><type>57</type>'
    "<refermsg><displayname>甲</displayname><content>原话在这里</content>"
    "<createtime>1792000000</createtime></refermsg></appmsg></msg>"
)
IMAGE_REF = (
    '<msg><appmsg><title>这是哪</title><type>57</type>'
    "<refermsg><displayname>乙</displayname>"
    '<content>&lt;msg&gt;&lt;img md5="bffee8b9477c936098967b7f048b53a6"&gt;&lt;/img&gt;&lt;/msg&gt;</content>'
    "<createtime>1792116838</createtime></refermsg></appmsg></msg>"
)


class QuotedReferTests(unittest.TestCase):
    def test_sticker_refer_id_yields_md5_and_hides_raw_id(self):
        info = bridge_main.parse_appmsg(STICKER_REF)
        self.assertEqual(info["refer_md5"], "fb0dbcea25affabed88336bb23f520d1")
        self.assertEqual(info["refer_content"], "")      # 原始 ID 不能进正文
        self.assertEqual(info["refer_time"], "1792116879")
        self.assertEqual(info["refer_author"], "小泽布尔")

    def test_plain_text_refer_still_carries_quoted_text(self):
        info = bridge_main.parse_appmsg(TEXT_REF)
        self.assertIsNone(info["refer_md5"])
        self.assertEqual(info["refer_content"], "原话在这里")

    def test_image_refer_still_yields_md5(self):
        info = bridge_main.parse_appmsg(IMAGE_REF)
        self.assertEqual(info["refer_md5"], "bffee8b9477c936098967b7f048b53a6")
        self.assertEqual(info["refer_content"], "")


if __name__ == "__main__":
    unittest.main()
