# coding:utf-8
"""按账号隔离「运行时进度」状态（多账号一条龙专用）。

## 解决的问题

上游把「任务进度」与「用户配置」混存在同一份 `config.yaml` 里，其中最典型的是
若干 `*_timestamp`：任务打满后写入当前时间，下次执行时用
`Date.is_next_mon_x_am(timestamp, refresh_hour)` 判断「本周是否还没打过」。

多账号一条龙只切换了游戏注册表，没有切换这份状态，于是：

    账号 A 打满 18000 → 写 weekly_divergent_timestamp = T
    账号 B 读到同一个 T → 判定「尚未刷新」→ 静默跳过，整周不做

同类受害键还有 `already_used_codes`（A 兑过的兑换码 B 不再兑）、`power_plan`
（A 扣掉的次数 B 也看不到）等，共见 `STATE_KEYS`。

## 做法：状态镜像（零上游代码改动）

不改 `module/config/config.py`，而是由编排器在每个账号的子进程前后做一次「镜像」：

    ㈠ 读出该账号的进度快照 → ㈡ 写进 config.yaml → ㈢ 跑子进程
    → ㈣ 子进程退出后把进度抽回 state/{uid}.yaml

子进程看到的始终是一份正常的 `config.yaml`，无需感知任何新机制；上游更新时
`config.py` / `main.py` 一行都不用改，因此不会与上游产生新的合并冲突。

## 为什么是「镜像」而不是「切换配置文件」

实测过的替代方案及其失败原因：

- 同进程内构造多个 `Config(config_path=...)`：被 `SingletonMeta` 拦截，第二个
  实例拿到的仍是第一个的 `config_path`。
- 子进程切工作目录（`cwd=acctA`）：被 `main.py` 开头的
  `os.chdir(脚本目录)` 抵消，两个账号仍读同一份 `config.yaml`。
- 把 `config.yaml` 换成符号链接再切指向：隔离确实生效，但任何用
  `os.replace` 原子重写该文件的进程（打包版 GUI、多数编辑器）都会把链接
  变成普通文件，指向关系静默丢失、无任何报错。

## 边界

- 只在多账号一条龙内生效；手动运行 `python main.py <task>` 完全不经过本模块，
  行为与改造前一致。
- 状态文件写在应用根目录的 `state/`（运行数据，不入仓库）。
- 未知键会被上游 `_update_config` 丢弃，因此本模块只写 `config.example.yaml`
  中已存在的键，不新增任何配置键。
"""
import os
import shutil
from typing import Any, Dict

from module.config import cfg
from module.logger import log

_LOG_TAG = "[多账号一条龙]"

STATE_DIR_NAME = "state"
BASELINE_FILE_NAME = "_baseline.yaml"

# 运行时进度键：由任务自己写入、且语义上是「某个账号的进度」。
# 新增键时必须同步这里，否则该键会退回共享语义（不会报错，但隔离失效）。
# 枚举方式见知识库 m7a-per-account-state-isolation.md 的 D5：凡被
# cfg.set_value / cfg.save_timestamp 写过的键都应列入。
STATE_KEYS = (
    # 每周 / 每月 / 每日任务进度
    "weekly_divergent_timestamp",       # 差分宇宙积分奖励（18000）
    "currencywars_timestamp",           # 货币战争积分奖励（18000）
    "universe_timestamp",               # 模拟宇宙 / 差分宇宙固定次数
    "last_run_timestamp",               # 每日实训
    "echo_of_war_timestamp",            # 历战余响
    "forgottenhall_timestamp",          # 忘却之庭
    "purefiction_timestamp",            # 虚构叙事
    "apocalyptic_timestamp",            # 末日幻影
    "fight_timestamp",                  # 锄大地
    "weekly_relic_cleanup_timestamp",   # 每周遗器清理（上游 v2026.10.16 起）
    "asset_self_molding_resin_timestamp",
    "asset_ember_special_pass_timestamp",
    "asset_ember_regular_pass_timestamp",
    "asset_ember_tracks_of_destiny_timestamp",
    # 差分宇宙固定次数计数
    "divergent_universe_daily_completed_count",
    "divergent_universe_daily_completed_timestamp",
    "divergent_universe_weekly_completed_count",
    "divergent_universe_weekly_completed_timestamp",
    # 其余运行时状态
    "already_used_codes",   # 已使用兑换码（跨账号共享会造成「A 兑过 B 不再兑」）
    "daily_tasks",          # 每日实训任务清单（子进程结束会清空，仍按账号隔离以免串号）
    "power_plan",           # 体力计划剩余次数（A 扣次数会同步吃掉 B 的计划）
)


def state_dir() -> str:
    return os.path.abspath(STATE_DIR_NAME)


def state_path(account_id) -> str:
    """某账号的状态文件路径。"""
    return os.path.join(state_dir(), f"{account_id}.yaml")


def baseline_path() -> str:
    """编排器启动时 config.yaml 中状态键的原始值（用于收尾恢复与崩溃兜底）。"""
    return os.path.join(state_dir(), BASELINE_FILE_NAME)


def _known_keys() -> frozenset:
    """只保留 config.example.yaml 里真实存在的键——上游会丢弃模板中不存在的键。"""
    return frozenset(key for key in STATE_KEYS if key in cfg.config)


def _reload() -> None:
    """从磁盘重读 config.yaml 到内存。

    **必须在每次 capture() 前执行**：子进程是一个独立进程，它用
    `cfg.set_value()` 写下的进度只落在磁盘上，父进程内存里仍是拉起前的旧值。
    而 `Config` 只在 `set_value` / `set_values` 时才会 `_load_config()`，
    父进程读完盘之后就再没重读过——不重读的话 `capture()` 拿到的是陈旧值，
    归档出去的状态永远等于镜像进来时的状态，这一轮的成果等于没记。

    `_load_config()` 内部调用 `_update_config()`，会用磁盘值覆盖内存同名键，
    且不会丢弃磁盘上新出现的键。
    """
    reload = getattr(cfg, "_load_config", None)
    if callable(reload):
        reload(save=False)


def capture() -> Dict[str, Any]:
    """抽取当前 config.yaml 中的状态键（先重读磁盘，深拷贝由 get_value 保证）。"""
    _reload()
    return {key: cfg.get_value(key) for key in _known_keys()}


def apply(values: Dict[str, Any]):
    """把一份状态写进 config.yaml（只写已知的状态键，其余忽略）。"""
    known = _known_keys()
    payload = {key: value for key, value in (values or {}).items() if key in known}
    if not payload:
        return
    cfg.set_values(payload)


def _read_yaml(path: str) -> Dict[str, Any]:
    try:
        from ruamel.yaml import YAML
        with open(path, "r", encoding="utf-8") as file:
            loaded = YAML().load(file)
        return loaded if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # 损坏的状态文件不应阻断整条龙
        log.warning(f"{_LOG_TAG} 读取状态文件失败，按空状态处理：{path}（{exc}）")
        return {}


def _write_yaml(path: str, payload: Dict[str, Any]):
    """原子写状态文件（先写临时文件再 replace）。"""
    from ruamel.yaml import YAML
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    yaml = YAML()
    with open(tmp_path, "w", encoding="utf-8") as file:
        yaml.dump(dict(payload), file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp_path, path)


def defaults() -> Dict[str, Any]:
    """模板默认值（新账号首次运行时使用）。"""
    from module.config import EXAMPLE_PATH
    return _read_yaml(EXAMPLE_PATH)


def load(account_id) -> Dict[str, Any]:
    """读某账号的状态；文件不存在时回退模板默认值。"""
    path = state_path(account_id)
    if os.path.exists(path):
        stored = _read_yaml(path)
        if stored:
            return stored
    return defaults()


def save(account_id, values: Dict[str, Any]) -> str:
    """把状态落盘到 state/{uid}.yaml，返回路径。"""
    path = state_path(account_id)
    known = _known_keys()
    payload = {key: value for key, value in (values or {}).items() if key in known}
    _write_yaml(path, payload)
    return path


def persist_baseline():
    """记录编排开始前 config.yaml 的状态键原值（收尾恢复用）。"""
    _write_yaml(baseline_path(), capture())


def restore_baseline() -> bool:
    """把 config.yaml 的状态键恢复为编排开始前的值；没有基线时返回 False。"""
    path = baseline_path()
    if not os.path.exists(path):
        return False
    apply(_read_yaml(path))
    return True


def clear_baseline():
    try:
        os.remove(baseline_path())
    except OSError:
        pass


def recover_stale_baseline() -> bool:
    """上一次编排异常中断的兜底：发现残留基线则先恢复并清理。

    返回 True 表示执行了恢复（调用方应记一条日志/台账）。
    """
    if not os.path.exists(baseline_path()):
        return False
    restored = restore_baseline()
    clear_baseline()
    return restored


def reset(account_id=None) -> int:
    """删除某账号（或全部）的状态文件，返回删除数量。仅供测试与手动重置。"""
    directory = state_dir()
    if not os.path.isdir(directory):
        return 0
    if account_id is not None:
        targets = [state_path(account_id)]
    else:
        targets = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".yaml") and name != BASELINE_FILE_NAME
        ]
    removed = 0
    for path in targets:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed
