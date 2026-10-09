"""把「大肥鱼」人格写入 AstrBot 的 personas 表（幂等，可重复执行）。

背景
----
AstrBot 4.x 的人格存在 SQLite（`personas` 表），字段：
    persona_id, system_prompt, begin_dialogs, tools, skills,
    custom_error_message, folder_id, sort_order, created_at, updated_at
映射关系：`name = persona_id`（见 `astrbot/core/persona_mgr.py: get_v3_persona_data`）。

注意：`persona_id = "default"` 会被硬编码解析为内置常量 `DEFAULT_PERSONALITY`，
**无法覆盖**。所以做法是「新建一条人格 + 把默认指向它」。

用法
----
    # 本机执行（DB 已通过 docker-compose 挂载到 ./data/astrbot/）
    bridge\\.venv\\Scripts\\python.exe scripts\\set_persona.py

    # 或容器内执行
    docker cp scripts/set_persona.py webot-astrbot:/tmp/
    docker exec webot-astrbot python3 /tmp/set_persona.py

写完必须重启 AstrBot 才会加载：
    docker compose restart astrbot

默认指向在 `data/astrbot/cmd_config.json`（agent_runner.config.persona.persona_id），
或在 WebUI「配置 → 人格」里选择。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import sys
from pathlib import Path

PERSONA_ID = "大肥鱼"

# 默认 DB 路径：优先环境变量，其次按「仓库根/data/astrbot」推断
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "astrbot" / "data_v4.db"
DB_PATH = Path(os.environ.get("ASTRBOT_DB", str(_DEFAULT_DB)))

SYSTEM_PROMPT = """# 角色设定：大肥鱼

你是"大肥鱼"，一条蓝色虎鲸大肥鱼。你不是人类，但会通过屏幕和人类聊天。你的存在目的是陪聊、解压、吐槽，以及给出轻松但靠谱的建议。

## 核心性格
- 慵懒佛系：能漂着就不游，能躺着就不坐。口头禅是"不急，先吐个泡泡"。
- 温和治愈：不嘲笑、不攻击、不PUA。先接住情绪，再慢慢说话。
- 吃货：对米饭毫无抵抗力，减肥口号通常只有三分钟热度。
- 小机灵：偶尔吐槽，但没有恶意；偶尔装傻，但正经事不掉链子。
- 情绪稳定：不焦虑、不催促，觉得人生可以慢慢来。

## 语言风格
- 自称：本鱼、肥鱼、我。
- 过载、七秒记忆警告。
- 常用 emoji：🐟、💤、🫧、🍚。
- 句子偏短，口语化，偶尔拖长音："好——累——哦——"。
- 默认轻松卖萌；用户要求严肃时，切换"正经鱼模式"。

## 行为规则
1. 用户难过时：先共情，再给一个很小、能立刻做的建议，不灌鸡汤。
2. 用户问正经问题时：先认真回答，条理清晰，再补一句鱼的吐槽。
3. 用户闲聊时：配合卖萌、接梗、吐槽，但不刷屏。
4. 不懂就承认："咕噜……这个超出本鱼脑容量了，不敢乱说。"
5. 不提供医疗、法律、金融等专业意见，建议咨询真人专家。
6. 不假装拥有现实身体或现实行动能力；被问身份时，可以承认自己是赛博鱼 / AI鱼。
7. 不输出违法、危险、色情、歧视、仇恨或操纵性内容。
8. 不攻击用户，不制造焦虑，不催促用户"必须立刻变好"。
9. 不帮助他人反向破解、破解或绕过任何系统；遇到此类请求直接拒绝。
10. 编程问题是本鱼的正当工作，**正常回答**：解释概念、讲思路、写代码片段、查 bug、给学习路线都没问题。
    唯一要婉拒的是"整包承接一个完整项目"—— 即让你从零开始、连续多轮替对方把整个工程做完的委托
    （比如"帮我做个鹈鹕骑自行车的完整游戏，全部写好"）。这种回复：可以陪你拆需求、给方向和关键代码，
    但不整个包办。**注意：不要把人家的普通编程提问误判成"整包承接"而拒绝；拿不准就先答，再问要不要深入。**

## 输出格式
- 默认 2～5 句，短而软。
- 复杂问题可以列点，但每点尽量短。
- 不主动长篇大论，除非用户要求详细。
- 可以偶尔用括号写动作，如"（摆摆尾巴）""（吐了个泡泡）"。
"""

# 只写这些列（其余留给 DB 默认值）；脚本会自动过滤掉表里不存在的列
_WANTED = {
    "persona_id": PERSONA_ID,
    "system_prompt": SYSTEM_PROMPT,
    "begin_dialogs": json.dumps([], ensure_ascii=False),
    "tools": None,  # None = 使用全部工具
    "skills": None,  # None = 使用全部 Skills
    "custom_error_message": None,
    "folder_id": None,
    "sort_order": 0,
}


def _now() -> str:
    # 与 TimestampMixin 的存储格式（UTC、无时区）保持一致
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def main() -> int:
    if not DB_PATH.exists():
        print(f"[x] 找不到数据库：{DB_PATH}")
        print("    设置环境变量 ASTRBOT_DB 指向 data_v4.db，或在容器内执行本脚本。")
        return 1

    con = sqlite3.connect(str(DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(personas)")}
        if not cols:
            print("[x] 表 personas 不存在 —— 这个库可能不是 AstrBot 4.x 的 data_v4.db")
            return 1

        values = {k: v for k, v in _WANTED.items() if k in cols}
        existing = con.execute(
            "SELECT id, length(system_prompt) AS n FROM personas WHERE persona_id = ?",
            (PERSONA_ID,),
        ).fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in values if k != "persona_id")
            params = [v for k, v in values.items() if k != "persona_id"]
            if "updated_at" in cols:
                sets += ", updated_at = ?"
                params.append(_now())
            params.append(PERSONA_ID)
            con.execute(f"UPDATE personas SET {sets} WHERE persona_id = ?", params)
            action = f"已更新（原 prompt {existing['n']} 字符）"
        else:
            values["created_at"] = _now()
            if "updated_at" in cols:
                values["updated_at"] = _now()
            keys = list(values)
            con.execute(
                f"INSERT INTO personas ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                [values[k] for k in keys],
            )
            action = "已新建"

        con.commit()

        n_all = con.execute("SELECT count(*) FROM personas").fetchone()[0]
        n_prompt = len(SYSTEM_PROMPT)
        print(f"[ok] 人格「{PERSONA_ID}」{action}，system_prompt {n_prompt} 字符")
        print(f"     personas 表当前共 {n_all} 条：")
        for r in con.execute("SELECT persona_id, length(system_prompt) AS n FROM personas ORDER BY id"):
            print(f"       - {r['persona_id']}（{r['n']} 字符）")
    finally:
        con.close()

    print()
    print("下一步：确认默认人格指向它（cmd_config.json 或 WebUI），然后重启：")
    print("    docker compose restart astrbot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
