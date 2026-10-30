# coding:utf-8
"""按账号隔离运行时进度（tasks/multi_account/state.py）的回归测试。

不触达真实 config.yaml、注册表与游戏：用临时目录承载 state/，
并用桩替换 cfg 的读写，验证镜像逻辑本身。
"""
import os

import pytest

from tasks.multi_account import state


class _MemoryCfg:
    """最小的 cfg 替身：只实现 state 模块用到的四个接口。"""

    def __init__(self, values=None, example=None):
        self.config = dict(example if example is not None else values or {})
        self._values = dict(values or {})

    def get_value(self, key, default=None):
        return self._values.get(key, default)

    def set_values(self, values):
        self._values.update(values)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 state 目录指到临时路径，并用内存 cfg 替换真实配置。"""
    monkeypatch.setattr(state, "state_dir", lambda: str(tmp_path / "state"))
    example = tmp_path / "config.example.yaml"
    example.write_text(
        "weekly_divergent_timestamp: 0\n"
        "currencywars_timestamp: 0\n"
        "already_used_codes: []\n"
        "power_plan: []\n"
        "refresh_hour: 4\n",
        encoding="utf-8",
    )
    cfg = _MemoryCfg(
        values={
            "weekly_divergent_timestamp": 0,
            "currencywars_timestamp": 0,
            "already_used_codes": [],
            "power_plan": [],
        },
        example={"weekly_divergent_timestamp": 0, "currencywars_timestamp": 0,
                 "already_used_codes": [], "power_plan": [], "refresh_hour": 4},
    )
    monkeypatch.setattr(state, "cfg", cfg)
    monkeypatch.setattr(state, "EXAMPLE_PATH", str(example), raising=False)
    monkeypatch.setattr("module.config.EXAMPLE_PATH", str(example), raising=False)
    return cfg, tmp_path


class TestStateKeys:
    def test_weekly_keys_listed(self):
        """两个每周积分任务必须在隔离清单里，否则本次修复无效。"""
        assert "weekly_divergent_timestamp" in state.STATE_KEYS
        assert "currencywars_timestamp" in state.STATE_KEYS

    def test_already_used_codes_listed(self):
        assert "already_used_codes" in state.STATE_KEYS

    def test_state_path_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state, "state_dir", lambda: str(tmp_path / "state"))
        assert state.state_path(106535892).endswith("106535892.yaml")


class TestCaptureApply:
    def test_capture_only_state_keys(self, env):
        cfg, _ = env
        cfg._values["refresh_hour"] = 4          # 用户配置，不应被抽取
        captured = state.capture()
        assert "weekly_divergent_timestamp" in captured
        assert "refresh_hour" not in captured

    def test_apply_ignores_unknown_keys(self, env):
        cfg, _ = env
        state.apply({"weekly_divergent_timestamp": 123.0, "not_a_state_key": "x"})
        assert cfg.get_value("weekly_divergent_timestamp") == 123.0
        assert cfg.get_value("not_a_state_key") is None

    def test_apply_empty_is_noop(self, env):
        cfg, _ = env
        cfg._values["weekly_divergent_timestamp"] = 999.0
        state.apply({})
        assert cfg.get_value("weekly_divergent_timestamp") == 999.0


class TestSaveLoad:
    def test_load_missing_falls_back_to_defaults(self, env):
        loaded = state.load(122149825)
        assert loaded.get("weekly_divergent_timestamp") == 0
        assert loaded.get("already_used_codes") == []

    def test_save_then_load_roundtrip(self, env):
        state.save(122149825, {"weekly_divergent_timestamp": 1793000000.0,
                               "already_used_codes": ["CODE_B"]})
        loaded = state.load(122149825)
        assert loaded["weekly_divergent_timestamp"] == 1793000000.0
        assert list(loaded["already_used_codes"]) == ["CODE_B"]

    def test_accounts_are_isolated(self, env):
        state.save(111, {"weekly_divergent_timestamp": 100.0, "already_used_codes": ["A"]})
        state.save(222, {"weekly_divergent_timestamp": 200.0, "already_used_codes": ["B"]})
        a = state.load(111)
        b = state.load(222)
        assert a["weekly_divergent_timestamp"] == 100.0
        assert b["weekly_divergent_timestamp"] == 200.0
        assert list(a["already_used_codes"]) == ["A"]
        assert list(b["already_used_codes"]) == ["B"]

    def test_save_drops_unknown_keys(self, env):
        path = state.save(111, {"weekly_divergent_timestamp": 1.0, "junk_key": "x"})
        assert "junk_key" not in open(path, encoding="utf-8").read()

    def test_save_is_atomic_leaves_no_tmp(self, env):
        path = state.save(111, {"weekly_divergent_timestamp": 1.0})
        assert not os.path.exists(f"{path}.tmp")


class TestBaseline:
    def test_persist_and_restore(self, env):
        cfg, _ = env
        cfg._values["weekly_divergent_timestamp"] = 500.0
        state.persist_baseline()

        cfg._values["weekly_divergent_timestamp"] = 999.0   # 模拟某个账号跑完写入
        assert state.restore_baseline() is True
        assert cfg.get_value("weekly_divergent_timestamp") == 500.0

    def test_restore_without_baseline(self, env):
        assert state.restore_baseline() is False

    def test_clear_baseline(self, env):
        state.persist_baseline()
        state.clear_baseline()
        assert not os.path.exists(state.baseline_path())

    def test_recover_stale_baseline(self, env):
        cfg, _ = env
        cfg._values["weekly_divergent_timestamp"] = 700.0
        state.persist_baseline()
        cfg._values["weekly_divergent_timestamp"] = 1234.0

        assert state.recover_stale_baseline() is True
        assert cfg.get_value("weekly_divergent_timestamp") == 700.0
        assert not os.path.exists(state.baseline_path())

    def test_recover_when_no_baseline(self, env):
        assert state.recover_stale_baseline() is False


class TestReset:
    def test_reset_single_account(self, env):
        state.save(111, {"weekly_divergent_timestamp": 1.0})
        state.save(222, {"weekly_divergent_timestamp": 2.0})
        assert state.reset(111) == 1
        assert not os.path.exists(state.state_path(111))
        assert os.path.exists(state.state_path(222))

    def test_reset_all_keeps_baseline(self, env):
        state.save(111, {"weekly_divergent_timestamp": 1.0})
        state.persist_baseline()
        assert state.reset() == 1
        assert os.path.exists(state.baseline_path())


class TestMirrorScenario:
    """端到端复现本次缺陷场景：A 打满后 B 必须仍被判定为「需要执行」。"""

    def test_second_account_not_skipped(self, env, monkeypatch):
        cfg, _ = env

        # 账号 A：打满差分宇宙，写入时间戳
        state.apply(state.load(111))
        cfg._values["weekly_divergent_timestamp"] = 1793000000.0
        state.save(111, state.capture())

        # 账号 B：镜像自己的状态后再看 —— 必须是「未完成」
        state.apply(state.load(222))
        assert cfg.get_value("weekly_divergent_timestamp") == 0

        # 反向：A 的状态不能被 B 覆盖
        assert state.load(111)["weekly_divergent_timestamp"] == 1793000000.0

    def test_already_used_codes_not_shared(self, env):
        cfg, _ = env

        state.apply(state.load(111))
        cfg._values["already_used_codes"] = ["CODE_A"]
        state.save(111, state.capture())

        state.apply(state.load(222))
        assert cfg.get_value("already_used_codes") == []
