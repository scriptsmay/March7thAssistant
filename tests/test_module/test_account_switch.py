# coding:utf-8
"""「切换账号」核心模块（module/account）与流程编排步骤的回归测试。

不触达真实注册表与游戏：账号目录扫描使用 tmp_path 隔离；
switch_account 仅测试本地可判定的参数校验分支。
"""
import sys

import pytest

import module.account as account_module
from module.account import Reason
from module.workflow import STEP_TYPE_LABELS, normalize_step, summarize_step


class TestAccountScan:
    def test_list_exported_accounts(self, tmp_path):
        (tmp_path / "123456.reg").write_bytes(b"x")
        (tmp_path / "654321.reg").write_bytes(b"y")
        (tmp_path / "654321.name").write_text("小号", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")
        accounts = account_module.list_exported_accounts(base_dir=str(tmp_path))
        assert [a.account_id for a in accounts] == [123456, 654321]
        assert accounts[1].display_name == "小号"
        assert accounts[0].display_name == "123456"

    def test_resolve_label_fallback(self, tmp_path):
        assert account_module.resolve_account_label(999, base_dir=str(tmp_path)) == "999"

    def test_missing_dir_returns_empty(self, tmp_path):
        assert account_module.list_exported_accounts(base_dir=str(tmp_path / "nope")) == []


class TestSwitchAccountValidation:
    def test_invalid_id(self):
        result = account_module.switch_account("not-a-number")
        assert result.ok is False
        assert result.reason == Reason.ACCOUNT_NOT_FOUND

    @pytest.mark.skipif(sys.platform != "win32", reason="目录校验分支仅在 Windows 可达")
    def test_missing_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(account_module, "ACCOUNTS_DIR_NAME", str(tmp_path / "missing"))
        result = account_module.switch_account(123)
        assert result.ok is False
        assert result.reason in (Reason.ACCOUNTS_DIR_MISSING, Reason.CLOUD_NOT_SUPPORTED)


class TestSwitchAccountStep:
    def test_label_registered(self):
        assert STEP_TYPE_LABELS.get("switch_account") == "切换账号"

    def test_normalize_defaults(self):
        step = normalize_step({"type": "switch_account"})
        assert step["account_id"] == ""
        assert step["restart_game"] is True
        assert step["switch_timeout"] == 120

    def test_normalize_values(self):
        step = normalize_step({
            "type": "switch_account",
            "account_id": "123456",
            "restart_game": False,
            "switch_timeout": 60,
        })
        assert step["account_id"] == "123456"
        assert step["restart_game"] is False
        assert step["switch_timeout"] == 60

    def test_summarize(self):
        title, detail = summarize_step({"type": "switch_account", "account_id": "123456"})
        assert "切换账号" in title
        assert "123456" in title
        assert detail  # 非空（重启行为说明）

    def test_editor_step_types_cover_labels(self):
        pytest.importorskip("PySide6")
        pytest.importorskip("qfluentwidgets")
        from app.workflow_interface import StepEditDialog
        assert set(STEP_TYPE_LABELS.keys()) <= set(StepEditDialog.STEP_TYPES)
