#!/usr/bin/env python3
"""自评 LLM 参数纠偏与「未找到名为 X 的模型」自愈的单元测试（v1.2.3）。

覆盖：
  1. 默认配置（两项都空）→ 不传任何参数，且不产生 llm.get_available_models 额外 RPC；
  2. review_task 正常透传；
  3. review_model 填了**任务名**（planner）且 Host 任务清单里确实有 → 调用前自动改按任务名；
  4. review_model 填了任务名但 Host 任务清单为空（模型列表为空）→ 不纠偏，原样传 model；
  5. review_model 填的是真实模型名 → 原样传 model，且不查任务清单（零额外 RPC）；
  6. 报「未找到名为 X 的模型」→ 首次即完整诊断，并停用被拒参数；
  7. 同类错误第二次不再重复完整诊断（防刷屏），但仍有停用提示；
  8. 被拒的是 task_name 时停用 task_name，下次不传；
  9. on_config_update 清空自愈状态，让改完配置立即生效。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

try:  # tomllib 需要 Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 低版本解释器
    tomllib = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = "org.mai-mai.repeater-recall"
DEFAULT_TASKS = ["utils", "replyer", "planner"]

# 真机 config.toml 原文（2026-09-16）：review_model 与 review_task **双填**了任务名 "planner"
REAL_CONFIG_TOML = """
[plugin]
enabled = true
config_version = "1"
[repeat]
enabled = true
threshold = 5
cooldown_seconds = 3600
min_length = 2
ignore_case = true
require_distinct_users = true
[recall]
enabled = true
self_review = true
review_delay_seconds = 3
review_model = "planner"
context_messages = 10
max_recalls_per_hour = 10
admin_ids = ["123456789"]
review_task = "planner"
review_timeout_seconds = 60
group_window_seconds = 6.0
"""


def _build(review_task: str = "", review_model: str = "", returns: dict | None = None, host=None):
    """装配一个已 on_load 的插件实例，返回 (plugin, host)。"""
    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    host = host or FakeHost(plugin_id=PLUGIN_ID, returns=returns)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    config = get_default_config(getattr(type(plugin), "config_model", None))
    config["recall"]["review_task"] = review_task
    config["recall"]["review_model"] = review_model
    bind_context(plugin, ctx, config)
    asyncio.run(plugin.on_load())
    return plugin, host


def _build_raw(config: dict, returns: dict | None = None, host=None):
    """用一份完整配置字典装配插件（真机 config.toml 回归用）。"""
    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    host = host or FakeHost(plugin_id=PLUGIN_ID, returns=returns)
    ctx = build_context(PLUGIN_ID, rpc_call=host.rpc_call)
    bind_context(plugin, ctx, config)
    asyncio.run(plugin.on_load())
    return plugin, host


def _resolve(plugin) -> dict:
    return asyncio.run(plugin._resolve_review_llm_kwargs())


def _slow_host(delay: float) -> FakeHost:
    """构造一个「查询可用任务名会卡住」的假宿主，验证超时保护。"""
    host = FakeHost(plugin_id=PLUGIN_ID)
    original = host.rpc_call

    async def _rpc(method, plugin_id="", payload=None, **kwargs):
        cap = dict(payload or {}).get("capability") or method
        if cap == "llm.get_available_models":
            await asyncio.sleep(delay)
        return await original(method, plugin_id, payload, **kwargs)

    host.rpc_call = _rpc
    return host


def _fail(plugin, err: str) -> None:
    asyncio.run(plugin._on_review_failure({"success": False, "error": err}))


def _llm_rpcs(host) -> list[str]:
    return [cap for cap, _ in host.calls if cap.startswith("llm.")]


class TestResolveParams:
    def test_default_config_sends_nothing_and_no_extra_rpc(self):
        plugin, host = _build()
        assert _resolve(plugin) == {}
        assert _llm_rpcs(host) == []  # 默认配置绝不产生额外 RPC

    def test_review_task_passthrough(self):
        plugin, host = _build(review_task="utils")
        assert _resolve(plugin) == {"task_name": "utils"}
        assert _llm_rpcs(host) == []

    def test_review_model_task_name_auto_corrected(self, caplog):
        plugin, host = _build(review_model="planner",
                              returns={"llm.get_available_models": DEFAULT_TASKS})
        with caplog.at_level(logging.WARNING):
            kwargs = _resolve(plugin)
        assert kwargs == {"task_name": "planner"}  # 改按任务名，且不传 model
        assert "recall.review_task" in caplog.text  # 提示用户把该值挪到 review_task
        assert "planner" in DEFAULT_TASKS
        # 纠偏必须只查一次任务清单（懒加载 + TTL 缓存）
        assert _llm_rpcs(host).count("llm.get_available_models") == 1

    def test_correction_warns_only_once(self, caplog):
        plugin, _ = _build(review_model="planner",
                           returns={"llm.get_available_models": DEFAULT_TASKS})
        _resolve(plugin)
        plugin._task_names_cache = None  # 清缓存逼出第二次查询，验证 warning 去重
        with caplog.at_level(logging.WARNING):
            kwargs = _resolve(plugin)
        assert kwargs == {"task_name": "planner"}
        assert caplog.text.count("模型任务名而不是具体模型名") == 0  # 已提示过，不再刷

    def test_no_correction_when_host_task_list_empty(self):
        # 模型列表为空时无法确认，保持原样传 model（Host 会给出「模型列表为空」类诊断）
        plugin, _ = _build(review_model="planner", returns={"llm.get_available_models": []})
        assert _resolve(plugin) == {"model": "planner"}

    def test_real_model_name_untouched_and_no_extra_rpc(self):
        plugin, host = _build(review_model="deepseek-v4-flash")
        assert _resolve(plugin) == {"model": "deepseek-v4-flash"}
        assert _llm_rpcs(host) == []  # 白名单左侧短路：不查任务清单

    def test_both_fields_set_real_values(self):
        plugin, _ = _build(review_task="utils", review_model="deepseek-v4-flash")
        assert _resolve(plugin) == {"task_name": "utils", "model": "deepseek-v4-flash"}


class TestModelNameError:
    def test_first_hit_reports_and_disables_review_model(self, caplog):
        plugin, _ = _build(review_model="planner", returns={"llm.get_available_models": []})
        plugin._last_review_llm_kwargs = {"model": "planner"}
        with caplog.at_level(logging.ERROR):
            _fail(plugin, "未找到名为 'planner' 的模型")
        assert "未找到名为" in caplog.text and "planner" in caplog.text
        assert plugin._review_model_disabled is True
        # 停用后不再传出 model（回落任务路由），自评不会反复失败
        assert _resolve(plugin) == {}

    def test_second_hit_does_not_repeat_full_diagnosis(self, caplog):
        plugin, _ = _build(review_model="planner", returns={"llm.get_available_models": []})
        plugin._last_review_llm_kwargs = {"model": "planner"}
        _fail(plugin, "未找到名为 'planner' 的模型")
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            _fail(plugin, "未找到名为 'planner' 的模型")
        assert "MaiBot 1.2.5 起两个参数是分开的" not in caplog.text

    def test_rejected_task_name_disables_task_field(self):
        plugin, _ = _build(review_task="planner", returns={"llm.get_available_models": []})
        plugin._last_review_llm_kwargs = {"task_name": "planner"}
        _fail(plugin, "未找到名为 'planner' 的模型")
        assert plugin._task_name_disabled is True
        assert _resolve(plugin) == {}

    def test_error_name_matching_nothing_disables_nothing(self):
        plugin, _ = _build(review_task="utils")
        plugin._last_review_llm_kwargs = {"task_name": "utils"}
        _fail(plugin, "未找到名为 'ghost-model' 的模型")
        assert plugin._task_name_disabled is False
        assert plugin._review_model_disabled is False
        assert _resolve(plugin) == {"task_name": "utils"}  # 配置本身没问题，不动它

    def test_unrelated_error_does_not_touch_params(self):
        plugin, _ = _build(review_task="utils")
        plugin._last_review_llm_kwargs = {"task_name": "utils"}
        _fail(plugin, "TimeoutError: 自评 LLM 调用超时")
        assert plugin._review_model_disabled is False and plugin._task_name_disabled is False

    def test_streak_still_breaks_circuit_at_three(self):
        plugin, _ = _build(review_task="utils")
        plugin._last_review_llm_kwargs = {"task_name": "utils"}
        for _ in range(2):
            _fail(plugin, "TimeoutError: 自评 LLM 调用超时")
        assert plugin._review_fail_streak == 2
        _fail(plugin, "TimeoutError: 自评 LLM 调用超时")
        assert plugin._review_fail_streak == 0  # 第 3 次熔断并清零
        assert plugin._review_paused_until > 0

    def test_config_update_resets_self_heal_state(self):
        plugin, _ = _build(review_model="planner", returns={"llm.get_available_models": []})
        plugin._last_review_llm_kwargs = {"model": "planner"}
        _fail(plugin, "未找到名为 'planner' 的模型")
        assert plugin._review_model_disabled is True
        asyncio.run(plugin.on_config_update("plugin", {}, "2"))
        assert plugin._review_model_disabled is False
        assert plugin._model_err_reported is False
        assert plugin._task_names_cache is None


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 才能解析真机 config.toml")
class TestRealMachineConfigRegression:
    """真机 config.toml（2026-09-16）回归：`review_model` / `review_task` 双填任务名 planner。"""

    @staticmethod
    def _cfg() -> dict:
        return tomllib.loads(REAL_CONFIG_TOML)

    def test_double_filled_task_name_drops_model_before_call(self, caplog):
        # Host 认得 planner 任务 → 调用前就丢掉误填的 model，自评一次失败都不会发生
        plugin, host = _build_raw(
            self._cfg(), returns={"llm.get_available_models": DEFAULT_TASKS}
        )
        with caplog.at_level(logging.WARNING):
            kwargs = _resolve(plugin)
        assert kwargs == {"task_name": "planner"}
        assert "review_task" in caplog.text and "已忽略" in caplog.text

    def test_review_task_wins_over_task_name_in_model(self):
        # review_task 是显式指定的任务，不该被 review_model 里的任务名覆盖
        config = self._cfg()
        config["recall"]["review_task"] = "utils"
        plugin, _ = _build_raw(config, returns={"llm.get_available_models": DEFAULT_TASKS})
        assert _resolve(plugin) == {"task_name": "utils"}

    def test_empty_model_list_fails_once_then_falls_back(self):
        # 模型列表为空 → 无法确认，先原样传（失败一次），失败后两个参数都停用，回落默认任务
        plugin, _ = _build_raw(self._cfg(), returns={"llm.get_available_models": []})
        assert _resolve(plugin) == {"task_name": "planner", "model": "planner"}
        plugin._last_review_llm_kwargs = {"task_name": "planner", "model": "planner"}
        _fail(plugin, "未找到名为 'planner' 的模型")
        assert plugin._review_model_disabled is True
        assert plugin._task_name_disabled is True
        assert _resolve(plugin) == {}

    def test_diagnose_helper_degrades_gracefully(self):
        plugin, _ = _build(returns={"llm.get_available_models": []})
        assert "模型列表为空" in asyncio.run(plugin._diagnose_llm_models())
        plugin2, _ = _build(returns={"llm.get_available_models": DEFAULT_TASKS})
        assert "planner" in asyncio.run(plugin2._diagnose_llm_models())

    def test_task_names_query_timeout_does_not_hang_review(self, monkeypatch):
        # Host RPC 无内建超时：诊断调用卡住时按「查不到」降级，绝不阻塞自评流程
        plugin, _ = _build(review_model="planner", host=_slow_host(delay=5))
        monkeypatch.setattr(sys.modules["plugin_under_test"], "_TASK_NAMES_TIMEOUT", 0.05)
        assert asyncio.run(plugin._available_task_names()) == []
        assert "超时" in plugin._task_names_error
        assert _resolve(plugin) == {"model": "planner"}  # 查不到清单 → 不纠偏，原样传


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
