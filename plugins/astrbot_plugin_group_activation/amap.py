"""AMap Web Service client; fixed endpoints, bounded calls, no key in outputs."""
import asyncio
import json
import math
import re


class AMapClient:
    def __init__(self, key, quota, daily_limit=100, is_current=lambda: True):
        self.key, self.quota, self.daily_limit = key, quota, daily_limit
        self.is_current = is_current

    async def request(self, path, **params):
        if not self.is_current():
            raise ValueError("地图请求已取消")
        if not self.key:
            raise ValueError("尚未配置高德 Web 服务 API Key")
        ticket = await self.quota.reserve(self.daily_limit)
        if ticket is None:
            raise ValueError("今日高德请求额度已用完")
        outcome = "failed"
        try:
            import aiohttp
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get("https://restapi.amap.com/v3/" + path,
                                       params={**params, "key": self.key, "output": "JSON"},
                                       allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f"高德服务返回 HTTP {response.status}")
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(16384):
                        size += len(chunk)
                        if size > 500000:
                            raise ValueError("高德返回内容过大")
                        chunks.append(chunk)
                    data = json.loads(b"".join(chunks))
            if not isinstance(data, dict) or data.get("status") != "1":
                code = str(data.get("infocode", "unknown")) if isinstance(data, dict) else "unknown"
                raise ValueError("高德请求未成功，错误码 " + re.sub(r"[^a-zA-Z0-9]", "", code)[:20])
            outcome = "success"
            return data
        finally:
            await asyncio.shield(self.quota.finish(ticket, outcome))

    async def places(self, city, keywords):
        if not city.strip() or not keywords.strip():
            raise ValueError("请提供城市和景点/地点关键词")
        data = await self.request("place/text", city=city[:40], keywords=keywords[:100],
                                  citylimit="true", offset=5, page=1, extensions="base")
        return [{key: str(poi.get(key, ""))[:250] for key in ("id", "name", "address", "location", "cityname")}
                for poi in data.get("pois", [])[:5] if isinstance(poi, dict)]

    async def weather(self, city, forecast=False):
        city = city.strip()
        if not city or len(city) > 40:
            raise ValueError("请先提供要查询天气的城市或区县，不能根据群名猜测位置")
        if re.fullmatch(r"\d{6}", city):
            adcode = city
        else:
            data = await self.request("config/district", keywords=city, subdistrict=0, extensions="base")
            districts = [d for d in data.get("districts", []) if isinstance(d, dict)]
            candidates = [d for d in districts if str(d.get("name", "")).removesuffix("市") == city.removesuffix("市")]
            if len(candidates) != 1:
                choices = [{"name": d.get("name"), "adcode": d.get("adcode")} for d in districts[:5]]
                raise ValueError("城市不明确或未找到，请提供具体城市/区县或行政区编码：" + json.dumps(choices, ensure_ascii=False))
            district = candidates[0]
            if district.get("level") not in ("city", "district") and district.get("name") not in ("北京市", "上海市", "天津市", "重庆市"):
                raise ValueError("请提供具体城市或区县，不能查询整个省的天气")
            adcode = str(district.get("adcode", ""))
            if not re.fullmatch(r"\d{6}", adcode):
                raise ValueError("高德返回无效行政区编码")
        data = await self.request("weather/weatherInfo", city=adcode, extensions="all" if forecast else "base")
        entries = data.get("forecasts" if forecast else "lives") or []
        entries = [item for item in entries if isinstance(item, dict) and str(item.get("adcode")) == adcode]
        if not entries:
            raise ValueError("高德没有返回该城市的天气数据，请稍后再试，不要改用百度搜索或编造天气")
        item = entries[0]
        fields = ("province", "city", "adcode", "reporttime") if forecast else (
            "province", "city", "adcode", "reporttime", "weather", "temperature", "winddirection", "windpower", "humidity")
        result = {key: str(item.get(key, ""))[:100] for key in fields}
        if forecast:
            keys = ("date", "week", "dayweather", "nightweather", "daytemp", "nighttemp", "daywind", "nightwind", "daypower", "nightpower")
            result["casts"] = [{key: str(day.get(key, ""))[:100] for key in keys}
                               for day in item.get("casts", [])[:4] if isinstance(day, dict)]
        return {"source": "高德天气", "forecast": bool(forecast), **result,
                "note": "以 reporttime 为更新时间；预报仅覆盖返回日期，不推测远期假期天气。温度单位℃，湿度单位%。"}

    async def location(self, name, city):
        results = await self.places(city, name)
        exact = [item for item in results if item["name"] == name and item["location"]]
        if len(exact) == 1:
            return exact[0]
        if len(results) == 1 and results[0]["location"]:
            return results[0]
        if not results:
            raise ValueError("未找到地点：" + name)
        raise ValueError("地点名称不明确，请先选定具体地点和地址：" +
                         json.dumps(results, ensure_ascii=False))

    async def route(self, city, stops, mode="driving"):
        if mode not in ("driving", "walking", "transit"):
            raise ValueError("交通方式仅支持 driving、walking、transit")
        if not city.strip() or not 2 <= len(stops) <= 6 or any(not str(s).strip() or len(str(s)) > 100 for s in stops):
            raise ValueError("请提供城市以及按游览顺序排列的 2～6 个地点")
        # Cross-city transit requires cityd and is deliberately not guessed.
        places = [await self.location(str(stop).strip(), city) for stop in stops]
        for place in places:
            try:
                longitude, latitude = map(float, place["location"].split(","))
                valid = (math.isfinite(longitude) and math.isfinite(latitude) and
                         -180 <= longitude <= 180 and -90 <= latitude <= 90)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise ValueError("高德返回无效坐标，已停止路线查询")
        legs = []
        for origin, destination in zip(places, places[1:]):
            params = {"origin": origin["location"], "destination": destination["location"]}
            path = "direction/" + mode
            if mode == "transit":
                path += "/integrated"
                params.update(city=city[:40], cityd=city[:40])
            data = await self.request(path, **params)
            route = data.get("route") or {}
            options = route.get("transits" if mode == "transit" else "paths") or []
            if not options:
                raise ValueError("高德未找到这一段可用路线")
            choice = options[0]
            instructions = [str(step.get("instruction", ""))[:200] for step in choice.get("steps", [])[:8]]
            if mode == "transit":
                instructions = []
                for segment in choice.get("segments", [])[:8]:
                    lines = (segment.get("bus") or {}).get("buslines") or []
                    instructions.extend(str(line.get("name", ""))[:200] for line in lines[:1])
            legs.append({"from": origin["name"], "to": destination["name"],
                         "distance_m": choice.get("distance"), "duration_s": choice.get("duration"),
                         "instructions": instructions})
        return {"source": "高德地图", "city": city, "mode": mode, "places": places, "legs": legs,
                "note": "路线按用户给定顺序查询，非全局最优；交通耗时不含游玩、排队、用餐。请确认日期、预算和偏好再排日程，门票及开放时间需另查。"}
