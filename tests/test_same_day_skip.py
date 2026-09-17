#!/usr/bin/env python3
"""当天复读去重（v1.3.0）的单元测试。

覆盖：
  1. 当天同一句第二次攒够阈值 → 不再跟读，且不占用冷却；
  2. 其他句子不受影响（只屏蔽「已经复读过的那些句子」）；
  3. 跨天（本地 0 点）惰性重置 → 隔天可以重新复读；
  4. 默认 scope=stream：A 群复读过的句子，B 群当天仍可复读；
  5. scope=global：同一句当天全局只复读一次；
  6. skip_same_day_repeat=false → 行为回到 v1.2.x（同句可反复复读）；
  7. 落盘 + 重启恢复：新实例读回同一份 data 目录后当天记忆仍在；
  8. 隔天的记录文件不恢复；坏 JSON 不崩加载；
  9. 发送失败不计入当天记录（失败不该让一句失去当天资格）；
 10. 单日记录上限裁剪，防极端刷屏撑内存。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = "org.mai-mai.repeater-recall"
STORE_NAME = "repeat_same_day.json"
TEXT = "好耶"


def _build(repeat: dict | None = None, paths: FakePaths | None = None, host: FakeHost | None = None):
    """装配一个已 on_load 的插件，返回 (plugin, host, paths)。"""
    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    paths = paths or FakePaths()
    host = host or FakeHost(plugin_id=PLUGIN_ID, paths=paths)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call, paths=paths)
    config = get_default_config(getattr(type(plugin), "config_model", None))
    config["repeat"]["threshold"] = 3
    config["repeat"]["cooldown_seconds"] = 0  # 测试里连续触发，绕开冷却干扰
    config["repeat"].update(repeat or {})
    bind_context(plugin, ctx, config)
    asyncio.run(plugin.on_load())
    return plugin, host, paths


def _msg(text: str, uid: str, sid: str = "stream-1") -> dict:
    return {
        "session_id": sid,
        "processed_plain_text": text,
        "user_id": uid,
        "message_info": {"user_info": {"user_id": uid}},
    }


async def _feed(plugin, items, sid: str = "stream-1") -> None:
    """喂入 (文本, 用户) 序列，并等后台跟读任务全部结束。"""
    for text, uid in items:
        await plugin.handle_repeat_check(_msg(text, uid, sid))
    for _ in range(5):  # 跟读是 spawn 的后台任务，收拢干净再断言
        pending = [t for t in list(plugin._tasks) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


def _chain(text: str = TEXT):
    """三个人接龙同一句（默认要求不同用户才触发跟读）。"""
    return [(text, "u1"), (text, "u2"), (text, "u3")]


def _flaky_send_host(paths: FakePaths, fail_times: int = 1) -> FakeHost:
    """前 fail_times 次 send.text 返回失败，之后成功。"""
    host = FakeHost(plugin_id=PLUGIN_ID, paths=paths)
    original = host.rpc_call
    state = {"left": fail_times}

    async def _rpc(method, plugin_id="", payload=None, **kwargs):
        kw = dict(payload or {})
        cap = kw.get("capability") or method
        if cap == "send.text" and state["left"] > 0:
            state["left"] -= 1
            # 与 FakeHost 的记录方式一致：这次调用确实发生了（只是被判失败）
            host.calls.append((cap, dict(kw.get("args") or {})))
            return {"sent": False, "message_id": None}
        return await original(method, plugin_id, payload, **kwargs)

    host.rpc_call = _rpc
    return host


# ---------------------------------------------------------------- 用例


def test_same_day_second_round_is_skipped():
    """同一天内同一句第二次攒够阈值 → 不再复读。"""
    plugin, host, _paths = _build()
    asyncio.run(_feed(plugin, _chain()))
    assert host.sent_texts == [TEXT], f"首轮应复读一次：{host.sent_texts}"

    asyncio.run(_feed(plugin, _chain()))
    assert host.sent_texts == [TEXT], f"同一天第二轮不该复读：{host.sent_texts}"
    assert len(plugin._same_day) == 1


def test_other_texts_still_repeat():
    """只屏蔽已复读过的句子，其他句子照常。"""
    plugin, host, _paths = _build()
    asyncio.run(_feed(plugin, _chain()))
    asyncio.run(_feed(plugin, _chain()))  # 被拦
    asyncio.run(_feed(plugin, _chain(text="另一句")))
    asyncio.run(_feed(plugin, _chain(text="再来一句")))
    assert host.sent_texts == [TEXT, "另一句", "再来一句"], host.sent_texts


def test_skipped_repeat_does_not_burn_cooldown():
    """被拦时不进冷却：没真正跟读，就不该占用其他句子的复读机会。"""
    plugin, host, _paths = _build(repeat={"cooldown_seconds": 300})
    asyncio.run(_feed(plugin, _chain()))
    plugin._cooldown_until.clear()  # 让首轮冷却先过期，单独验证「被拦是否重新设冷却」
    asyncio.run(_feed(plugin, _chain()))
    assert "stream-1" not in plugin._cooldown_until, "被拦不该重新进冷却"
    asyncio.run(_feed(plugin, _chain(text="另一句")))
    assert host.sent_texts == [TEXT, "另一句"], host.sent_texts


def test_cross_day_reset():
    """模拟跨天：日期一变即清空，隔天可重新复读。"""
    plugin, host, _paths = _build()
    asyncio.run(_feed(plugin, _chain()))
    assert len(plugin._same_day) == 1

    plugin._same_day_date = "1970-01-01"  # 假装记录属于昨天
    asyncio.run(_feed(plugin, _chain()))
    assert host.sent_texts == [TEXT, TEXT], f"隔天应恢复复读：{host.sent_texts}"


def test_today_str_format():
    """日期键为本地时区 YYYY-MM-DD。"""
    plugin, _host, _paths = _build()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", plugin._today_str())


def test_scope_stream_keeps_chats_independent():
    """默认 scope=stream：别的群不受本群当天记录影响。"""
    plugin, host, _paths = _build()
    asyncio.run(_feed(plugin, _chain(), sid="group-A"))
    asyncio.run(_feed(plugin, _chain(), sid="group-B"))
    assert host.sent_texts == [TEXT, TEXT], "各群应各记各的"


def test_scope_global_shares_across_chats():
    """scope=global：同一句当天全局只复读一次。"""
    plugin, host, _paths = _build(repeat={"same_day_scope": "global"})
    asyncio.run(_feed(plugin, _chain(), sid="group-A"))
    asyncio.run(_feed(plugin, _chain(), sid="group-B"))
    assert host.sent_texts == [TEXT], f"全局范围下不该复用同一句：{host.sent_texts}"
    # 归一化后视为同一句（尾随空格 / 大小写差异不另算一句）
    asyncio.run(_feed(plugin, _chain(text="好耶 "), sid="group-C"))
    assert host.sent_texts == [TEXT], host.sent_texts


def test_invalid_scope_falls_back_to_stream():
    """scope 写错值 → 按 stream 处理（不至于让全站静音）。"""
    plugin, host, _paths = _build(repeat={"same_day_scope": "GROUP"})
    assert plugin._same_day_scope_name() == "stream"
    asyncio.run(_feed(plugin, _chain(), sid="group-A"))
    asyncio.run(_feed(plugin, _chain(), sid="group-B"))
    assert host.sent_texts == [TEXT, TEXT], host.sent_texts


def test_disabled_restores_legacy_behavior():
    """关闭开关 → 回到 v1.2.x：同句可反复复读。"""
    plugin, host, _paths = _build(repeat={"skip_same_day_repeat": False})
    asyncio.run(_feed(plugin, _chain()))
    asyncio.run(_feed(plugin, _chain()))
    assert host.sent_texts == [TEXT, TEXT], host.sent_texts
    assert plugin._same_day == {}, "关掉开关就不该再记账"


def test_persist_and_restore_after_restart():
    """落盘 + 重启恢复：新实例读回同一 data 目录后当天记忆仍在。"""
    paths = FakePaths()
    plugin, host, _paths = _build(paths=paths)
    asyncio.run(_feed(plugin, _chain()))
    store = paths.data_dir / STORE_NAME
    assert store.exists(), "复读成功后应落盘"
    saved = json.loads(store.read_text(encoding="utf-8"))
    assert saved["date"] == plugin._same_day_date
    assert saved["scope"] == "stream"

    # 模拟 MaiBot 重启：同一 data 目录上装配新实例
    plugin2, host2, _p2 = _build(paths=paths)
    assert len(plugin2._same_day) == 1, "重启后应恢复当天记录"
    asyncio.run(_feed(plugin2, _chain()))
    assert host2.sent_texts == [], f"重启后同一天不该重复复读：{host2.sent_texts}"


def test_stale_file_is_ignored():
    """隔天的记录文件不恢复（否则相当于永久静音）。"""
    paths = FakePaths()
    # 手工写一份「昨天」的记录，再加载
    (paths.data_dir / STORE_NAME).write_text(
        json.dumps({"date": "1970-01-01", "scope": "stream",
                    "entries": {"stream-1\x1f" + TEXT: 1.0}}, ensure_ascii=False),
        encoding="utf-8",
    )
    plugin, host, _paths = _build(paths=paths)
    assert plugin._same_day == {}, "隔天记录应被忽略"
    asyncio.run(_feed(plugin, _chain()))
    assert host.sent_texts == [TEXT], host.sent_texts


def test_broken_file_does_not_break_load():
    """坏 JSON 不影响加载，按空记录继续。"""
    paths = FakePaths()
    (paths.data_dir / STORE_NAME).write_text("{ 这不是 json", encoding="utf-8")
    plugin, host, _paths = _build(paths=paths)
    assert plugin._same_day == {}
    asyncio.run(_feed(plugin, _chain()))
    assert host.sent_texts == [TEXT], host.sent_texts


def test_send_failure_is_not_recorded():
    """发送失败不计入当天记录 —— 否则一次抖动就让该句当天失声。"""
    paths = FakePaths()
    host = _flaky_send_host(paths, fail_times=1)
    plugin, _host, _paths = _build(paths=paths, host=host)

    asyncio.run(_feed(plugin, _chain()))
    assert plugin._same_day == {}, "发送失败不该记账"

    asyncio.run(_feed(plugin, _chain()))
    assert len(plugin._same_day) == 1, "发送成功后应记账"
    assert len(host.sent_texts) == 2, host.sent_texts


def test_capacity_pruning_keeps_latest():
    """单日记录超上限时丢最旧的，内存不会无界增长。"""
    plugin, _host, _paths = _build()
    limit = load_plugin_module(PLUGIN_DIR)._SAME_DAY_MAX
    for i in range(limit + 20):
        plugin._same_day_remember("stream-1", f"句子{i}")
    assert len(plugin._same_day) == limit
    assert "stream-1\x1f句子0" not in plugin._same_day, "应丢最旧"
    assert f"stream-1\x1f句子{limit + 19}" in plugin._same_day, "应保留最新"


def test_key_uses_plain_text_not_builtin_hash():
    """键里是归一化文本本身：内置 hash() 带随机盐，跨进程会失配（落盘就白记）。"""
    plugin, _host, _paths = _build()
    assert plugin._same_day_key("stream-1", TEXT) == f"stream-1\x1f{TEXT}"
    # global 范围直接以文本为键（跨流共享）
    plugin2, _h2, _p2 = _build(repeat={"same_day_scope": "global"})
    assert plugin2._same_day_key("stream-1", TEXT) == TEXT
