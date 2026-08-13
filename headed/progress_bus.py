"""
注册进度总线：线程安全，供 main / controller 上报，dashboard 轮询展示。
项目本身没有可视化进度页，本模块是后加的本地状态层。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any


# 固定步骤顺序（面板进度条按此渲染）
STEP_DEFS: list[tuple[str, str]] = [
    ("queued", "排队等待"),
    ("browser", "启动浏览器"),
    ("open_signup", "打开注册页"),
    ("agree", "同意条款"),
    ("fill_email", "填写邮箱"),
    ("fill_password", "设置密码"),
    ("fill_birthday", "填写生日"),
    ("fill_name", "填写姓名"),
    ("captcha", "人机验证"),
    ("recovery", "辅助邮箱"),
    ("kmsi", "保持登录"),
    ("inbox", "进入账户"),
    ("save", "保存账号/Token"),
    ("done", "完成"),
]

STEP_LABELS = {k: v for k, v in STEP_DEFS}


class ProgressBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._run: dict[str, Any] = {
            "status": "idle",  # idle | running | stopping | finished
            "started_at": None,
            "finished_at": None,
            "max_tasks": 0,
            "concurrent": 0,
            "submitted": 0,
            "succeeded": 0,
            "failed": 0,
            "use_residential": None,
            "message": "",
        }
        self._logs: list[dict[str, Any]] = []
        self._seq = 0

    def reset_run(self, *, max_tasks: int, concurrent: int, use_residential: bool | None) -> None:
        with self._lock:
            self._jobs.clear()
            self._order.clear()
            self._logs.clear()
            self._seq = 0
            self._run = {
                "status": "running",
                "started_at": time.time(),
                "finished_at": None,
                "max_tasks": max_tasks,
                "concurrent": concurrent,
                "submitted": 0,
                "succeeded": 0,
                "failed": 0,
                "use_residential": use_residential,
                "message": "注册任务已启动",
            }

    def finish_run(self, message: str = "") -> None:
        with self._lock:
            self._run["status"] = "finished"
            self._run["finished_at"] = time.time()
            if message:
                self._run["message"] = message

    def set_run_message(self, message: str) -> None:
        with self._lock:
            self._run["message"] = message

    def create_job(self, email: str, password: str) -> str:
        job_id = uuid.uuid4().hex[:10]
        now = time.time()
        with self._lock:
            self._seq += 1
            job = {
                "id": job_id,
                "seq": self._seq,
                "email": email,
                "password": password,
                "status": "running",  # running | success | failed
                "step": "queued",
                "step_label": STEP_LABELS["queued"],
                "detail": "已创建任务",
                "created_at": now,
                "updated_at": now,
                "history": [
                    {
                        "step": "queued",
                        "label": STEP_LABELS["queued"],
                        "detail": "已创建任务",
                        "ts": now,
                    }
                ],
                "error": None,
                "account_line": None,
            }
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._run["submitted"] = len(self._order)
            self._append_log(f"#{self._seq} 创建 {email}@…", level="info")
        return job_id

    def step(self, job_id: str, step: str, detail: str = "") -> None:
        now = time.time()
        label = STEP_LABELS.get(step, step)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["step"] = step
            job["step_label"] = label
            job["detail"] = detail or label
            job["updated_at"] = now
            job["history"].append(
                {"step": step, "label": label, "detail": detail or label, "ts": now}
            )
            # 限制历史长度
            if len(job["history"]) > 40:
                job["history"] = job["history"][-40:]
            self._append_log(f"#{job.get('seq')} [{label}] {detail or ''}".strip(), level="info")

    def set_error(self, job_id: str, reason: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["error"] = reason
            job["detail"] = reason
            job["updated_at"] = time.time()

    def set_fail_kind(self, job_id: str, kind: str) -> None:
        """失败分类: captcha | form | oauth | other — 供调度做人机连败换 IP。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["fail_kind"] = (kind or "other").strip().lower()
            job["updated_at"] = time.time()

    def get_fail_kind(self, job_id: str) -> str:
        with self._lock:
            job = self._jobs.get(job_id) or {}
            return str(job.get("fail_kind") or "")

    def set_account_line(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job["account_line"] = line
            job["updated_at"] = time.time()

    def succeed(self, job_id: str, detail: str = "注册成功") -> None:
        self._finish_job(job_id, "success", "done", detail)

    def fail(self, job_id: str, detail: str = "注册失败") -> None:
        # 若已有更具体的 error/detail（来自 controller），不要被泛化文案覆盖
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                prev = (job.get("error") or job.get("detail") or "").strip()
                if prev and (
                    prev.startswith("失败原因")
                    or "验证码" in prev
                    or "FakeSuccess" in prev
                    or "异常" in prev
                    or "IP" in prev
                ):
                    if detail in ("注册失败（见上方步骤详情）", "注册流程返回失败", "注册失败"):
                        detail = prev
                job["error"] = detail
        self._finish_job(job_id, "failed", None, detail)

    def request_stop(self) -> None:
        with self._lock:
            self._run["status"] = "stopping"
            self._run["message"] = "正在停止并强杀浏览器…"
            self._append_log("收到停止请求，准备强杀浏览器", level="err")

    def abort_run(self, message: str = "已停止并强杀浏览器") -> None:
        """停止按钮专用：把仍在 running 的 job 标失败，并强制 run=finished。"""
        now = time.time()
        with self._lock:
            self._run["status"] = "finished"
            self._run["finished_at"] = now
            self._run["message"] = message
            for jid in list(self._order):
                job = self._jobs.get(jid)
                if not job:
                    continue
                if job.get("status") == "running":
                    job["status"] = "failed"
                    job["detail"] = "用户停止"
                    job["error"] = "用户停止 / 强杀浏览器"
                    job["updated_at"] = now
                    job["history"].append(
                        {
                            "step": job.get("step") or "browser",
                            "label": job.get("step_label") or "已停止",
                            "detail": "用户停止",
                            "ts": now,
                        }
                    )
                    self._run["failed"] = int(self._run.get("failed") or 0) + 1
            self._append_log(message, level="err")

    def successful_accounts(self) -> list[str]:
        with self._lock:
            lines = []
            for jid in self._order:
                job = self._jobs.get(jid)
                if not job or job.get("status") != "success":
                    continue
                line = job.get("account_line")
                if line:
                    lines.append(line)
                else:
                    email = job.get("email") or ""
                    password = job.get("password") or ""
                    if email:
                        lines.append(f"{email}: {password}".strip())
            return lines

    def _finish_job(self, job_id: str, status: str, step: str | None, detail: str) -> None:
        now = time.time()
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if step:
                job["step"] = step
                job["step_label"] = STEP_LABELS.get(step, step)
            job["status"] = status
            job["detail"] = detail
            job["updated_at"] = now
            job["history"].append(
                {
                    "step": job["step"],
                    "label": job["step_label"],
                    "detail": detail,
                    "ts": now,
                }
            )
            if status == "success":
                self._run["succeeded"] = int(self._run.get("succeeded") or 0) + 1
                self._append_log(f"#{job.get('seq')} 成功 {job.get('email')}", level="ok")
            else:
                self._run["failed"] = int(self._run.get("failed") or 0) + 1
                self._append_log(f"#{job.get('seq')} 失败 {detail}", level="err")

    def _append_log(self, message: str, level: str = "info") -> None:
        self._logs.append({"ts": time.time(), "level": level, "message": message})
        if len(self._logs) > 300:
            self._logs = self._logs[-300:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = [dict(self._jobs[i]) for i in self._order if i in self._jobs]
            # 最新的在前，方便面板
            jobs_sorted = sorted(jobs, key=lambda j: j.get("seq", 0), reverse=True)
            return {
                "run": dict(self._run),
                "steps": [{"key": k, "label": v} for k, v in STEP_DEFS],
                "jobs": jobs_sorted[:50],
                "logs": list(self._logs[-80:]),
                "server_time": time.time(),
            }


# 进程内单例
BUS = ProgressBus()
STOP_REQUESTED = threading.Event()


def report(job_id: str | None, step: str, detail: str = "") -> None:
    if not job_id:
        return
    try:
        # 去掉控制台/面板都可能炸的特殊符号
        if detail:
            detail = (
                str(detail)
                .replace("✅", "[OK]")
                .replace("❌", "[FAIL]")
                .replace("⚠", "[WARN]")
                .replace("→", "->")
                .replace("…", "...")
            )
        BUS.step(job_id, step, detail)
    except Exception:
        pass


def request_global_stop() -> None:
    STOP_REQUESTED.set()
    BUS.request_stop()


def clear_global_stop() -> None:
    STOP_REQUESTED.clear()


def should_stop() -> bool:
    return STOP_REQUESTED.is_set()
