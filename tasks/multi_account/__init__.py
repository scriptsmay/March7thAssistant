# coding:utf-8
"""多账号一条龙：按配置顺序对多个账号依次执行任务（默认「完整运行」）。

整体形态（增量设计，复用既有能力）：
- 每个账号的一次执行 = 启动一个子进程运行既有任务（默认 main/完整运行），
  与人工点一次「开始」完全等价；
- 账号之间的切换 = 注册表导入（module.account.switch_account）：关闭游戏 → 导入 → 校验；
- 编排器负责：顺序控制、单账号超时、失败重试、失败策略（继续/中止）、
  结束后回切首个账号、汇总通知、运行台账（logs/multi_account/）；
- 人工接管：沿用全局暂停（默认 F8）与停止（默认 F10）机制——暂停会同时停住
  当前子进程的动作与编排器推进，停止会终止整个进程树。

配置项见 assets/config/config.example.yaml「多账号一条龙」分节。
"""
import csv
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

import psutil

from module.account import (
    ensure_game_closed,
    get_current_account_id,
    list_exported_accounts,
    resolve_account_label,
    switch_account,
)
from module.config import cfg
from module.logger import log
from module.notification import notif
from module.notification.notification import NotificationLevel
from utils.console import pause_on_success
from utils.pause import pause_ctl

TASK_ID = "multiaccount"
DEFAULT_SUB_TASK = "main"
ALLOWED_SUB_TASKS = ("main", "routine", "daily", "power", "redemption")
LEDGER_DIR_NAME = os.path.join("logs", "multi_account")

_LOG_TAG = "[多账号一条龙]"


def _ledger_dir() -> str:
    return os.path.abspath(LEDGER_DIR_NAME)


def _log(message: str):
    log.info(f"{_LOG_TAG} {message}")


def _fmt_duration(seconds) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60}s"


class RunPlan:
    """一次多账号一条龙的运行计划（由 config.yaml 解析）。"""

    def __init__(self, accounts, sub_task, timeout_minutes, max_retries, on_failure,
                 restore_first, notify_summary):
        self.accounts = accounts          # 原始选择器（UID 字符串或账号名）
        self.resolved = []                # 校验后解析出的 UID 列表（validate_run_plan 填充）
        self.sub_task = sub_task
        self.timeout_minutes = timeout_minutes
        self.max_retries = max_retries
        self.on_failure = on_failure
        self.restore_first = restore_first
        self.notify_summary = notify_summary


def load_run_plan() -> RunPlan:
    """从 config.yaml 读取多账号一条龙配置。"""
    raw_accounts = cfg.get_value("multi_account_run_accounts", []) or []
    if not isinstance(raw_accounts, (list, tuple)):
        raw_accounts = [raw_accounts]
    accounts = []
    for item in raw_accounts:
        text = str(item).strip()
        if text:
            accounts.append(text)  # 保留原始选择器（UID 或账号名），校验/解析阶段统一处理

    return RunPlan(
        accounts=accounts,
        sub_task=str(cfg.get_value("multi_account_per_account_task", DEFAULT_SUB_TASK) or DEFAULT_SUB_TASK).strip(),
        timeout_minutes=int(cfg.get_value("multi_account_timeout_minutes", 120) or 0),
        max_retries=int(cfg.get_value("multi_account_max_retries", 1) or 0),
        on_failure=str(cfg.get_value("multi_account_on_failure", "continue") or "continue").strip().lower(),
        restore_first=bool(cfg.get_value("multi_account_restore_first_account", True)),
        notify_summary=bool(cfg.get_value("multi_account_notify_summary", True)),
    )


def resolve_account_selectors(selectors, exported_accounts=None) -> tuple[list[int], list[str]]:
    """把配置中的账号选择器（UID 或账号显示名）解析为 UID 列表。

    返回 (resolved_ids, errors)：resolved_ids 保持输入顺序；无法解析的条目逐条报错。
    """
    if exported_accounts is None:
        try:
            exported_accounts = list_exported_accounts()
        except Exception:
            exported_accounts = []

    by_name = {}
    for account in exported_accounts:
        by_name.setdefault(str(account.display_name), account.account_id)

    resolved: list[int] = []
    errors: list[str] = []
    for selector in selectors:
        text = str(selector).strip()
        if not text:
            continue
        if text.isdigit():
            resolved.append(int(text))
        elif text in by_name:
            resolved.append(by_name[text])
        else:
            available = "、".join(f"{account.display_name}({account.account_id})" for account in exported_accounts[:12]) or "无"
            errors.append(f"选择器「{text}」未匹配到已导出账号（现有：{available}）")
    return resolved, errors


def validate_run_plan(plan: RunPlan) -> list[str]:
    """校验运行计划，返回错误列表（空列表 = 可运行）。"""
    errors = []
    if not plan.accounts:
        errors.append("未配置账号：请先在「设置-账户」页导出账号，再到 config.yaml 的 multi_account_run_accounts 填写（可写 UID 或账号名）")

    try:
        exported_accounts = list_exported_accounts()
    except Exception:
        exported_accounts = []
    exported_ids = {account.account_id for account in exported_accounts}

    resolved, resolve_errors = resolve_account_selectors(plan.accounts, exported_accounts)
    plan.resolved = resolved
    errors.extend(resolve_errors)

    seen_ids = set()
    for account_id in resolved:
        if account_id in seen_ids:
            errors.append(f"账号 {account_id} 在 multi_account_run_accounts 中重复出现")
        seen_ids.add(account_id)
        if account_id not in exported_ids:
            errors.append(f"账号 {account_id} 尚未导出（缺少 settings/accounts/{account_id}.reg）")

    if plan.sub_task not in ALLOWED_SUB_TASKS:
        errors.append(f"子任务 {plan.sub_task!r} 不在允许列表 {list(ALLOWED_SUB_TASKS)}")
    if plan.max_retries < 0:
        errors.append("multi_account_max_retries 不能为负")
    if plan.timeout_minutes < 0:
        errors.append("multi_account_timeout_minutes 不能为负")
    if plan.on_failure not in ("continue", "abort"):
        errors.append("multi_account_on_failure 仅支持 continue / abort")

    try:
        if str(cfg.get_value("after_finish", "") or "") == "Loop":
            errors.append("「任务完成后」为 Loop 时不支持多账号一条龙，请先改为 Exit")
        if bool(cfg.get_value("cloud_game_enable", False)):
            errors.append("云游戏模式不支持多账号一条龙（账号切换依赖本地注册表）")
    except Exception:
        pass
    return errors


class Ledger:
    """运行台账：JSONL（机器可查）+ CSV（表格可查），均为追加写。"""

    CSV_COLUMNS = ["run_id", "time", "event", "account_id", "account_name",
                   "attempt", "result", "reason", "duration_s", "note"]

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.directory = _ledger_dir()
        os.makedirs(self.directory, exist_ok=True)
        self.jsonl_path = os.path.join(self.directory, "ledger.jsonl")
        self.csv_path = os.path.join(self.directory, "ledger.csv")

    def event(self, event, *, account_id=None, account_name="", attempt=None, ok=None,
              reason="", duration_s=None, note=""):
        row = {
            "run_id": self.run_id,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "account_id": account_id,
            "account_name": account_name,
            "attempt": attempt,
            "result": ("ok" if ok else "fail") if ok is not None else "",
            "reason": reason,
            "duration_s": duration_s,
            "note": note,
        }
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning(f"{_LOG_TAG} 台账（jsonl）写入失败：{exc}")
        try:
            csv_is_new = not os.path.exists(self.csv_path)
            with open(self.csv_path, "a", encoding=("utf-8-sig" if csv_is_new else "utf-8"), newline="") as f:
                writer = csv.writer(f)
                if csv_is_new:
                    writer.writerow(self.CSV_COLUMNS)
                writer.writerow([row[column] if row[column] is not None else "" for column in self.CSV_COLUMNS])
        except OSError as exc:
            log.warning(f"{_LOG_TAG} 台账（csv）写入失败：{exc}")


class ChildOutcome:
    def __init__(self, ok: bool, reason: str = "", tail: str = ""):
        self.ok = ok
        self.reason = reason
        self.tail = tail


def _build_child_command(sub_task: str) -> tuple[str, list[str]]:
    """构造子进程启动命令（与 GUI 启动内置任务同一形态）。"""
    if getattr(sys, "frozen", False):
        executable = os.path.abspath("./March7th Assistant.exe")
        if not os.path.exists(executable):
            raise RuntimeError("未找到可执行文件 March7th Assistant.exe")
        return executable, [executable, sub_task]
    main_script = os.path.abspath("main.py")
    return sys.executable, [sys.executable, main_script, sub_task]


def _kill_process_tree(pid: int):
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        try:
            parent.kill()
        except psutil.Error:
            pass
    except psutil.Error:
        pass


def _read_tail(path: str, max_lines: int = 15, max_bytes: int = 16384) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        lines = [line for line in data.splitlines() if line.strip()]
        return "\n".join(lines[-max_lines:])
    except OSError:
        return ""


def _wait_child(proc: subprocess.Popen, timeout_minutes: int, log_path: str) -> ChildOutcome:
    timeout_s = int(timeout_minutes) * 60 if timeout_minutes else 0
    started = time.monotonic()
    frozen = 0.0
    while True:
        if proc.poll() is not None:
            break
        if pause_ctl.is_paused():
            frozen += 1.0  # 暂停期间冻结超时计时（与 GUI 侧的计时冻结语义一致）
        time.sleep(1.0)
        if timeout_s and (time.monotonic() - started - frozen) > timeout_s:
            _log(f"单账号执行超时（>{timeout_minutes} 分钟），正在终止子进程…")
            _kill_process_tree(proc.pid)
            time.sleep(2)
            ensure_game_closed(timeout_s=120)
            return ChildOutcome(False, f"执行超时（>{timeout_minutes} 分钟）", _read_tail(log_path))

    code = proc.returncode
    if code == 0:
        return ChildOutcome(True)
    return ChildOutcome(False, f"子进程退出码 {code}", _read_tail(log_path))


def _run_child(plan: RunPlan, run_id: str, account_id: int, attempt: int) -> ChildOutcome:
    executable, args = _build_child_command(plan.sub_task)
    run_dir = os.path.join(_ledger_dir(), run_id)
    os.makedirs(run_dir, exist_ok=True)
    log_path = os.path.join(run_dir, f"{account_id}-attempt{attempt}.log")

    env = os.environ.copy()
    # 子进程每账号跑完即关闭游戏并退出；终局动作（关机等）由编排器统一处理
    env["MARCH7TH_AFTER_FINISH"] = "Exit"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    _log(f"启动子进程执行任务 {plan.sub_task}（日志：{os.path.relpath(log_path)}）")
    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(args, cwd=os.getcwd(), env=env,
                                stdout=log_file, stderr=subprocess.STDOUT)
        return _wait_child(proc, plan.timeout_minutes, log_path)


def _run_one_account(plan: RunPlan, ledger: Ledger, run_id: str, account_id: int, label: str) -> dict:
    """单个账号：切换（含重试）→ 子进程执行（含重试），返回结果字典。"""
    started = time.monotonic()
    attempt = 0
    last_reason = ""

    while attempt <= max(0, int(plan.max_retries)):
        attempt += 1
        pause_ctl.checkpoint()

        # 上一轮失败可能残留游戏进程，切换前先确保关闭
        if not ensure_game_closed(timeout_s=120):
            last_reason = "游戏在限时内未能关闭"
            ledger.event("account_error", account_id=account_id, account_name=label,
                         attempt=attempt, ok=False, reason="game_close_timeout")
            break

        switch_result = switch_account(account_id)
        ledger.event("account_switch", account_id=account_id, account_name=label,
                     attempt=attempt, ok=switch_result.ok, reason=switch_result.reason)
        if not switch_result.ok:
            last_reason = switch_result.message
            if switch_result.reason == "account_not_found":
                break  # 账号不存在，重试无意义
            continue

        child = _run_child(plan, run_id, account_id, attempt)
        duration = time.monotonic() - started
        ledger.event("account_run", account_id=account_id, account_name=label, attempt=attempt,
                     ok=child.ok, reason=child.reason, duration_s=round(duration, 1))
        if child.ok:
            return {"account_id": account_id, "label": label, "ok": True, "reason": "",
                    "attempts": attempt, "duration_s": duration, "duration_text": _fmt_duration(duration)}

        last_reason = child.reason
        _log(f"账号 {label} 第 {attempt} 次执行失败：{child.reason}")
        if child.tail:
            _log(f"子进程日志末尾：\n{child.tail}")
        ensure_game_closed(timeout_s=120)

    duration = time.monotonic() - started
    return {"account_id": account_id, "label": label, "ok": False, "reason": last_reason,
            "attempts": attempt, "duration_s": duration, "duration_text": _fmt_duration(duration)}


def _restore_account(ledger: Ledger, original_id: int):
    label = resolve_account_label(original_id)
    _log(f"回切到开始时的账号：{label}")
    result = switch_account(original_id)
    ledger.event("restore", account_id=original_id, account_name=label, ok=result.ok, reason=result.reason)
    if not result.ok:
        _log(f"回切账号失败：{result.message}")


def _summarize(plan: RunPlan, ledger: Ledger, results: list, ok_all: bool, run_id: str):
    ok_count = sum(1 for result in results if result["ok"])
    fail_count = len(results) - ok_count
    header = f"【多账号一条龙】{run_id} 结束：成功 {ok_count}/{len(plan.resolved) if plan.resolved else len(plan.accounts)}"
    body_lines = []
    for result in results:
        state = "成功" if result["ok"] else f"失败（{result['reason']}）"
        body_lines.append(f"- {result['label']}：{state}，耗时 {result['duration_text']}")
    _log(header + ("（全部成功）" if ok_all else "（存在失败）"))
    for line in body_lines:
        _log(line)
    ledger.event("run_end", ok=ok_all, note=f"ok={ok_count};fail={fail_count}")
    if plan.notify_summary:
        try:
            notif.notify(content="\n".join([header, *body_lines]), level=NotificationLevel.ALL)
            _log("汇总通知已发送")
        except Exception as exc:
            log.warning(f"{_LOG_TAG} 汇总通知发送失败：{exc}")


def start() -> bool:
    """多账号一条龙入口（由 main.py 分派调用）。返回是否全部成功。"""
    if sys.platform != "win32":
        log.error(f"{_LOG_TAG} 仅支持 Windows 本地游戏模式")
        return False

    plan = load_run_plan()
    errors = validate_run_plan(plan)
    if errors:
        for message in errors:
            log.error(f"{_LOG_TAG} {message}")
        return False

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    ledger = Ledger(run_id)
    account_ids = plan.resolved
    total = len(account_ids)
    _log(f"开始：run_id={run_id}，共 {total} 个账号，子任务={plan.sub_task}，"
         f"超时={plan.timeout_minutes if plan.timeout_minutes else '不限制'} 分钟/账号，"
         f"重试={plan.max_retries}，失败策略={plan.on_failure}")
    ledger.event("run_start", note=f"accounts={account_ids};task={plan.sub_task}")

    original_id = get_current_account_id()
    results: list = []
    ok_all = True

    try:
        if not ensure_game_closed(timeout_s=120):
            _log("开始前无法关闭正在运行的游戏，已中止")
            ledger.event("run_end", ok=False, reason="game_close_timeout")
            _summarize(plan, ledger, results, ok_all=False, run_id=run_id)
            return False

        for index, account_id in enumerate(account_ids, start=1):
            pause_ctl.checkpoint()
            label = resolve_account_label(account_id)
            _log(f"[{index}/{total}] 账号 {label}（{account_id}）：准备执行")

            result = _run_one_account(plan, ledger, run_id, account_id, label)
            results.append(result)

            if result["ok"]:
                _log(f"[{index}/{total}] 账号 {label}：完成（耗时 {result['duration_text']}）")
                continue

            ok_all = False
            _log(f"[{index}/{total}] 账号 {label}：失败 —— {result['reason']}")
            if plan.on_failure == "abort":
                for skipped_id in account_ids[index:]:
                    ledger.event("account_skipped", account_id=skipped_id,
                                 account_name=resolve_account_label(skipped_id),
                                 reason="on_failure=abort")
                _log(f"按策略（abort）停止，跳过剩余 {len(account_ids) - index} 个账号")
                break
    except KeyboardInterrupt:
        ok_all = False
        _log("收到中断信号，提前结束")
        ledger.event("run_aborted", reason="keyboard_interrupt")
    finally:
        if plan.restore_first and original_id is not None:
            pause_ctl.checkpoint()
            _restore_account(ledger, original_id)

    _summarize(plan, ledger, results, ok_all=ok_all, run_id=run_id)
    pause_on_success()
    return ok_all
