# coding:utf-8
"""多账号一条龙任务（tasks/multi_account）的回归测试。

不触达真实注册表与游戏：配置校验使用 FakeCfg 与桩账号列表；
子进程命令仅校验形态（开发态下应为 python main.py <task>）。
"""
import sys

import pytest

import tasks.multi_account as multi_account
from module.account import ExportedAccount
from tasks.multi_account import RunPlan, _fmt_duration, resolve_account_selectors, validate_run_plan


class _FakeCfg:
    def __init__(self, values=None):
        self.values = values or {}

    def get_value(self, key, default=None):
        return self.values.get(key, default)


def _make_plan(**overrides):
    params = {
        "accounts": ["123456789"],
        "sub_task": "main",
        "timeout_minutes": 120,
        "max_retries": 1,
        "on_failure": "continue",
        "restore_first": True,
        "notify_summary": True,
    }
    params.update(overrides)
    return RunPlan(**params)


class TestFormatDuration:
    def test_minutes_and_seconds(self):
        assert _fmt_duration(0) == "0m0s"
        assert _fmt_duration(59) == "0m59s"
        assert _fmt_duration(61) == "1m1s"
        assert _fmt_duration(3661) == "61m1s"


class TestResolveSelectors:
    def _accounts(self):
        return [
            ExportedAccount(111, "主号", "x.reg", 0.0),
            ExportedAccount(222, "小号", "y.reg", 0.0),
            ExportedAccount(333, "333", "z.reg", 0.0),
        ]

    def test_uid_and_name(self, monkeypatch):
        monkeypatch.setattr(multi_account, "list_exported_accounts", self._accounts)
        ids, errors = resolve_account_selectors(["主号", "222"])
        assert ids == [111, 222]
        assert errors == []

    def test_unknown_name_lists_available(self, monkeypatch):
        monkeypatch.setattr(multi_account, "list_exported_accounts", self._accounts)
        ids, errors = resolve_account_selectors(["不存在"])
        assert ids == []
        assert len(errors) == 1
        assert "未匹配到已导出账号" in errors[0]
        assert "主号(111)" in errors[0]


class TestValidateRunPlan:
    def _patch(self, monkeypatch, cfg_values=None,
               exported=(ExportedAccount(123456789, "A", "x.reg", 0.0),)):
        monkeypatch.setattr(multi_account, "cfg", _FakeCfg(
            cfg_values or {"after_finish": "Exit", "cloud_game_enable": False}))
        monkeypatch.setattr(multi_account, "list_exported_accounts", lambda: list(exported))

    def test_ok(self, monkeypatch):
        self._patch(monkeypatch)
        assert validate_run_plan(_make_plan()) == []

    def test_empty_accounts(self, monkeypatch):
        self._patch(monkeypatch)
        errors = validate_run_plan(_make_plan(accounts=[]))
        assert any("未配置账号" in e for e in errors)

    def test_account_not_exported(self, monkeypatch):
        self._patch(monkeypatch)
        errors = validate_run_plan(_make_plan(accounts=["1"]))
        assert any("尚未导出" in e for e in errors)

    def test_unknown_selector(self, monkeypatch):
        self._patch(monkeypatch)
        errors = validate_run_plan(_make_plan(accounts=["abc"]))
        assert any("未匹配到已导出账号" in e for e in errors)

    def test_name_selector_resolves(self, monkeypatch):
        self._patch(monkeypatch, exported=(
            ExportedAccount(111, "主号", "x.reg", 0.0),
            ExportedAccount(222, "小号", "y.reg", 0.0),
        ))
        plan = _make_plan(accounts=["主号", "222"])
        assert validate_run_plan(plan) == []
        assert plan.resolved == [111, 222]

    def test_duplicate_selector(self, monkeypatch):
        self._patch(monkeypatch)
        errors = validate_run_plan(_make_plan(accounts=["123456789", "123456789"]))
        assert any("重复" in e for e in errors)

    def test_bad_sub_task(self, monkeypatch):
        self._patch(monkeypatch)
        errors = validate_run_plan(_make_plan(sub_task="game"))
        assert any("不在允许列表" in e for e in errors)

    def test_bad_policy(self, monkeypatch):
        self._patch(monkeypatch)
        errors = validate_run_plan(_make_plan(on_failure="whatever"))
        assert any("continue / abort" in e for e in errors)

    def test_loop_rejected(self, monkeypatch):
        self._patch(monkeypatch, cfg_values={"after_finish": "Loop", "cloud_game_enable": False})
        assert any("Loop" in e for e in validate_run_plan(_make_plan()))

    def test_cloud_rejected(self, monkeypatch):
        self._patch(monkeypatch, cfg_values={"after_finish": "Exit", "cloud_game_enable": True})
        assert any("云游戏" in e for e in validate_run_plan(_make_plan()))


class TestChildCommand:
    def test_dev_command_shape(self):
        executable, args = multi_account._build_child_command("main")
        assert args[-1] == "main"
        assert args[0] == executable
        if not getattr(sys, "frozen", False):
            assert args[1].endswith("main.py")
