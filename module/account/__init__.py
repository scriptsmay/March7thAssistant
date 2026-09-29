# coding:utf-8
"""账号切换核心能力（本地游戏模式）。

把「多账号」的既有手动流程（GUI 账户设置里的 导出/导入）整理为可供
流程编排（workflow）、多账号一条龙任务与其它调用方复用的代码级能力：

- 账号数据集中存放于 `./settings/accounts/`（与 GUI 账户设置一致）：
  - `{uid}.reg`：游戏注册表导出（含登录会话），切换 = 导入对应文件；
  - `{uid}.name`：可读名称（可选，用于显示）；
- 切换时序：刷新当前账号导出（保持会话最新）→ 关闭游戏（如运行中）→
  导入目标注册表 → 回读 UID 校验；
- 约束：仅支持 Windows 本地游戏模式（与 GUI「账户设置」一致，不支持云游戏）。

日志按仓库约定固定中文原文（不走 tr）。
"""
import os
import time
from dataclasses import dataclass

from module.logger import log

# 账号导出目录（相对应用根目录；在调用时求值，避免导入期固化工作目录）
ACCOUNTS_DIR_NAME = os.path.join("settings", "accounts")


class Reason:
    """切换结果 reason 取值（供调用方分支与测试断言，勿依赖 message 文本）。"""

    OK = "ok"
    ALREADY_CURRENT = "already_current"          # 当前注册表已是目标账号，无需重新加载
    CLOUD_NOT_SUPPORTED = "cloud_not_supported"  # 云游戏模式不支持账号切换
    ACCOUNTS_DIR_MISSING = "accounts_dir_missing"
    ACCOUNT_NOT_FOUND = "account_not_found"      # 目标账号没有导出文件（或标识无效）
    GAME_CLOSE_TIMEOUT = "game_close_timeout"    # 游戏在限时内未能关闭
    VERIFY_FAILED = "verify_failed"              # 导入后回读校验不一致
    UNSUPPORTED_OS = "unsupported_os"
    UNEXPECTED_ERROR = "unexpected_error"


@dataclass
class ExportedAccount:
    """一个已导出的账号记录。"""

    account_id: int
    display_name: str
    reg_path: str
    exported_at: float


@dataclass
class SwitchResult:
    """账号切换结果。"""

    ok: bool
    reason: str
    message: str
    target_id: int | None = None
    previous_id: int | None = None
    reload_required: bool = False  # True 表示需重新启动游戏后目标账号才会生效


def _accounts_dir(base_dir: str | None = None) -> str:
    return os.path.abspath(base_dir or ACCOUNTS_DIR_NAME)


def resolve_account_label(account_id, base_dir: str | None = None) -> str:
    """账号显示名：优先 `{uid}.name`，缺省回退为 UID 字符串。"""
    directory = _accounts_dir(base_dir)
    name_file = os.path.join(directory, f"{account_id}.name")
    try:
        with open(name_file, "r", encoding="utf-8") as f:
            name = f.read().strip()
        if name:
            return name
    except OSError:
        pass
    return str(account_id)


def list_exported_accounts(base_dir: str | None = None) -> list[ExportedAccount]:
    """列出已导出的账号（扫描 `{uid}.reg`，按 UID 排序）。"""
    directory = _accounts_dir(base_dir)
    accounts: list[ExportedAccount] = []
    if not os.path.isdir(directory):
        return accounts
    for file_name in sorted(os.listdir(directory)):
        if not file_name.endswith(".reg"):
            continue
        stem = file_name[:-4]
        if not stem.isdigit():
            continue
        account_id = int(stem)
        reg_path = os.path.join(directory, file_name)
        accounts.append(ExportedAccount(
            account_id=account_id,
            display_name=resolve_account_label(account_id, base_dir=directory),
            reg_path=reg_path,
            exported_at=os.path.getmtime(reg_path),
        ))
    return accounts


def get_current_account_id() -> int | None:
    """读取当前注册表中的账号 UID（无会话/非 Windows 时返回 None）。"""
    try:
        from utils.registry import gameaccount
        return gameaccount.gamereg_uid()
    except Exception as exc:
        log.debug(f"读取当前账号 UID 失败：{exc}")
        return None


def refresh_current_account_record(base_dir: str | None = None) -> bool:
    """把当前注册表会话刷新到 `{uid}.reg`。

    仅当该账号已有导出文件时刷新（与 GUI 既有约定一致：不凭空新增账号条目）。
    """
    try:
        from utils.registry import gameaccount
        uid = gameaccount.gamereg_uid()
        if uid is None:
            return False
        reg_path = os.path.join(_accounts_dir(base_dir), f"{uid}.reg")
        if not os.path.exists(reg_path):
            return False
        gameaccount.gamereg_export(reg_path)
        return True
    except Exception as exc:
        log.warning(f"刷新当前账号导出失败：{exc}")
        return False


def _game_process_name() -> str:
    try:
        from module.config import cfg
        return str(cfg.game_process_name or "StarRail")
    except Exception:
        return "StarRail"


def _process_running(name: str) -> bool:
    if not name:
        return False
    import psutil
    lower = name.lower()
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            if lower in (proc.info.get("name") or "").lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def is_game_running() -> bool:
    """游戏是否在运行（窗口或进程任一存在即视为运行中）。"""
    try:
        from module.game import get_game_controller
        if get_game_controller().is_game_running():
            return True
    except Exception:
        pass
    return _process_running(_game_process_name())


def ensure_game_closed(timeout_s: int = 90) -> bool:
    """确保游戏已关闭；仍在运行则尝试关闭并等待。返回是否已关闭。"""
    if not is_game_running():
        return True

    log.info("检测到游戏正在运行，正在关闭游戏…")
    try:
        from module.game import get_game_controller
        get_game_controller().stop_game()
    except Exception as exc:
        log.warning(f"关闭游戏时发生错误：{exc}")

    deadline = time.monotonic() + max(1, int(timeout_s))
    while time.monotonic() < deadline:
        if not is_game_running():
            log.info("游戏已关闭")
            return True
        time.sleep(1)

    # 兜底：再按进程名直接终止一次（复用控制器的同名逻辑）
    try:
        from module.game.local import LocalGameController
        LocalGameController.terminate_named_process(_game_process_name(), termination_timeout=5)
    except Exception:
        pass
    for _ in range(5):
        if not is_game_running():
            return True
        time.sleep(1)

    log.error("游戏在限时内未能关闭")
    return False


def switch_account(account_id, *, close_timeout_s: int = 120, base_dir: str | None = None) -> SwitchResult:
    """切换到指定账号（导入注册表；不启动游戏）。

    约定：
    - 游戏必须在关闭状态下切换（调用前会先确保关闭，``close_timeout_s`` 为等待上限）；
    - 返回 ``SwitchResult``；``reload_required=True`` 表示需重新启动游戏才会生效；
    - 若当前已是目标账号，直接返回 ``ALREADY_CURRENT``（不关游戏、不导入）。
    """
    # 先做与平台无关的参数校验：无效标识在任何环境都直接报错
    try:
        target_id = int(account_id)
    except (TypeError, ValueError):
        return SwitchResult(False, Reason.ACCOUNT_NOT_FOUND, f"账号标识无效：{account_id}")

    import sys as _sys
    if _sys.platform != "win32":
        return SwitchResult(False, Reason.UNSUPPORTED_OS, "自动切换账号仅支持 Windows 本地游戏模式")

    try:
        from module.config import cfg
        if cfg.cloud_game_enable:
            return SwitchResult(False, Reason.CLOUD_NOT_SUPPORTED, "云游戏模式不支持自动切换账号")
    except Exception:
        pass

    directory = _accounts_dir(base_dir)
    if not os.path.isdir(directory):
        return SwitchResult(False, Reason.ACCOUNTS_DIR_MISSING, f"账号目录不存在：{directory}")

    target_reg = os.path.join(directory, f"{target_id}.reg")
    if not os.path.exists(target_reg):
        return SwitchResult(
            False, Reason.ACCOUNT_NOT_FOUND,
            f"账号 {target_id} 尚未导出，请先在「设置-账户」页导出该账号",
        )

    previous_id = get_current_account_id()
    if previous_id == target_id:
        return SwitchResult(
            True, Reason.ALREADY_CURRENT,
            f"当前已是账号 {resolve_account_label(target_id, base_dir=directory)}",
            target_id=target_id, previous_id=previous_id, reload_required=False,
        )

    # 保持当前账号的导出为最新（尽力而为，失败不影响切换）
    refresh_current_account_record(base_dir=base_dir)

    if not ensure_game_closed(timeout_s=int(close_timeout_s)):
        return SwitchResult(
            False, Reason.GAME_CLOSE_TIMEOUT,
            "游戏在限时内未能关闭，已取消切换",
            target_id=target_id, previous_id=previous_id,
        )

    last_error = ""
    for attempt in range(1, 3):
        try:
            from utils.registry import gameaccount
            gameaccount.gamereg_import(target_reg)
            last_error = ""
        except Exception as exc:
            last_error = str(exc)

        # 回读校验（注册表导入可能有短暂延迟，轮询等待）
        new_uid = None
        for _ in range(10):
            new_uid = get_current_account_id()
            if new_uid == target_id:
                break
            time.sleep(0.5)
        if new_uid == target_id:
            label = resolve_account_label(target_id, base_dir=directory)
            log.info(f"账号已切换：{label}（{previous_id} → {target_id}）")
            return SwitchResult(
                True, Reason.OK, f"已切换到账号 {label}",
                target_id=target_id, previous_id=previous_id, reload_required=True,
            )

        last_error = last_error or f"导入后回读 UID={new_uid}，与目标 {target_id} 不一致"
        log.warning(f"账号切换第 {attempt} 次尝试未通过校验：{last_error}")

    return SwitchResult(
        False, Reason.VERIFY_FAILED, f"切换校验失败：{last_error}",
        target_id=target_id, previous_id=previous_id,
    )
