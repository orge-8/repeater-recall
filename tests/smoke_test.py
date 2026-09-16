#!/usr/bin/env python3
"""FakeHost 生命周期冒烟：不启动 MaiBot 也能验证插件能加载、组件注册齐全。

    python tests/smoke_test.py

断言：
  1. create_plugin() / on_load() / on_config_update() / on_unload() 全程不抛；
  2. 组件清单与「组件名 → 方法名」一一对应（防止装饰器错位导致的静默漏注册）；
  3. on_load / on_config_update 阶段不产生任何 LLM RPC
     （自评参数纠偏必须是惰性的，默认配置下零额外开销）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = "org.mai-mai.repeater-recall"

# 组件名 → 方法名（组件名用 ASCII，中文触发词写在 pattern 里）
EXPECTED = {
    "command": {"recall": "cmd_recall"},
    "tool": {"recall_last_message": "recall_last_message"},
    "hook_handler": {
        "repeater_repeat_check": "handle_repeat_check",
        "repeater_track_outgoing": "handle_after_send",
    },
}


def _collect(plugin) -> dict[str, dict[str, str]]:
    """用 SDK 的 collect_components 收集真实注册信息（Runner 走的是同一个函数）。"""
    from maibot_sdk.components import collect_components

    seen: dict[str, dict[str, str]] = {}
    for component in collect_components(plugin):
        kind = str(component.get("type") or "").lower()
        name = str(component.get("name") or "")
        handler = str((component.get("metadata") or {}).get("handler_name") or "")
        seen.setdefault(kind, {})[name] = handler
    return seen


def main() -> int:
    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    host = FakeHost(plugin_id=PLUGIN_ID)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    bind_context(plugin, ctx, get_default_config(getattr(type(plugin), "config_model", None)))

    failures: list[str] = []
    counts: dict[str, int] = {}

    async def _run() -> None:
        await plugin.on_load()
        await plugin.on_config_update("plugin", {"plugin": {"config_version": "1"}}, "1")

        seen = _collect(plugin)
        for kind, expected in EXPECTED.items():
            actual = seen.get(kind, {})
            counts[kind] = len(actual)
            for name, method in expected.items():
                got = actual.get(name)
                if got is None:
                    failures.append(f"{kind} 未注册：{name}")
                elif got != method:
                    failures.append(f"{kind} {name} 绑定错位：期望 {method}，实际 {got}")

        await plugin.on_unload()

    asyncio.run(_run())

    llm_calls = [cap for cap, _ in host.calls if cap.startswith("llm.")]
    if llm_calls:
        failures.append(f"on_load/on_config_update 阶段不该调用 LLM：{llm_calls}")

    if failures:
        print("SMOKE FAIL")
        for item in failures:
            print("  -", item)
        return 1
    print(
        "SMOKE PASS（生命周期 ok；component {c} 个 / tool {t} 个 / hook_handler {h} 个；"
        "加载期无 LLM 调用）".format(
            c=counts.get("command", 0), t=counts.get("tool", 0), h=counts.get("hook_handler", 0)
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
