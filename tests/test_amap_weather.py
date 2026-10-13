import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_group_gate import Event, plugin_with_fake_astrbot
from plugins.astrbot_plugin_group_activation.amap import AMapClient
from plugins.astrbot_plugin_group_activation.routing import is_weather_request


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Plugin = plugin_with_fake_astrbot(self.tmp.name)
        self.bot = Plugin(types.SimpleNamespace())
        self.bot.cfg.l3_enable = self.bot.cfg.kb_enable = False

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, text):
        event = Event("a", text, at=True)
        self.bot._prepare_request_metadata(event, [self.bot._batch_item(event, text, True, 0)])
        return event
    async def test_weather_question_grants_amap_without_search_grant(self):
        event = self.prepare(text="深圳现在是什么天气？")
        self.assertEqual(event.get_extra("ga_weather_requests"), ["a"])
        self.assertEqual(event.get_extra("ga_search_requests"), [])
        with patch.object(AMapClient, "weather", AsyncMock(return_value={"city": "深圳市", "reporttime": "test"})) as call:
            self.assertIn("深圳市", await self.bot.amap_weather(event, "深圳"))
            call.assert_awaited_once_with("深圳", False)
            self.assertIn("没有", await self.bot.amap_weather(event, "深圳", "b"))
        result = await self.bot.web_search(event, "深圳天气")
        self.assertIn("amap_weather", result)
        self.assertFalse(self.bot._search_quota.path.exists())

    async def test_city_is_resolved_then_weather_uses_adcode(self):
        client = AMapClient("fake", self.bot._amap_quota)
        client.request = AsyncMock(side_effect=[
            {"districts": [{"name": "深圳市", "adcode": "440300", "level": "city"}]},
            {"lives": [{"city": "深圳市", "adcode": "440300", "weather": "晴", "temperature": "25", "reporttime": "test"}]}])
        result = await client.weather("深圳")
        self.assertEqual(result["weather"], "晴")
        self.assertEqual(result["reporttime"], "test")
        self.assertEqual(client.request.await_args_list[1].kwargs, {"city": "440300", "extensions": "base"})

    async def test_forecast_and_adcode_skip_district_query(self):
        client = AMapClient("fake", self.bot._amap_quota)
        client.request = AsyncMock(return_value={"forecasts": [{"adcode": "440300", "city": "深圳市", "reporttime": "test",
            "casts": [{"date": "2026-10-14", "dayweather": "晴", "daytemp": "25"}]}]})
        result = await client.weather("440300", True)
        self.assertEqual(result["casts"][0]["date"], "2026-10-14")
        client.request.assert_awaited_once_with("weather/weatherInfo", city="440300", extensions="all")

    async def test_missing_ambiguous_or_province_does_not_query_weather(self):
        client = AMapClient("fake", self.bot._amap_quota)
        client.request = AsyncMock()
        with self.assertRaisesRegex(ValueError, "提供"):
            await client.weather("")
        client.request.assert_not_awaited()
        for districts in ([{"name": "朝阳区", "adcode": "110105", "level": "district"},
                           {"name": "朝阳区", "adcode": "220104", "level": "district"}],
                          [{"name": "浙江省", "adcode": "330000", "level": "province"}]):
            client.request.reset_mock()
            client.request.return_value = {"districts": districts}
            with self.assertRaises(ValueError):
                await client.weather(districts[0]["name"])
            self.assertEqual(client.request.await_count, 1)

    async def test_no_data_never_falls_back_to_search(self):
        client = AMapClient("fake", self.bot._amap_quota)
        client.request = AsyncMock(return_value={"lives": []})
        with self.assertRaisesRegex(ValueError, "不要改用百度"):
            await client.weather("440300")

    async def test_weather_routing_ignores_quote_definition_and_cancel(self):
        for text in ("深圳天气怎么样", "明天北京下雨吗", "杭州气温", "要带伞吗"):
            self.assertTrue(is_weather_request(text))
        for text in ("有人说“深圳天气怎么样”", "天气是什么", "天气API怎么写", "不要查询天气"):
            self.assertFalse(is_weather_request(text))
