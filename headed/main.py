import os
import time
import json
from get_token import get_access_token
from concurrent.futures import ThreadPoolExecutor
from utils import random_email, generate_strong_password
from controllers.patchright_controller import PatchrightController
from controllers.playwright_controller import PlaywrightController
from controllers.base_controller import focus_browser_window
from progress_bus import BUS, clear_global_stop, should_stop

def show_banner():
    print("=" * 58)
    print("  Outlook / Hotmail Auto  v1.0.0")
    print("  Headed browser edition")
    print("  https://github.com/ZNaiGaomu/outlook-hotmail-auto")
    print("=" * 58)
    print()


def _infer_fail_kind(job_id) -> str:
    """从 BUS 任务详情推断失败类型：captcha | form | oauth | other。"""
    try:
        kind = BUS.get_fail_kind(job_id) if job_id else ""
        if kind:
            return kind
    except Exception:
        pass
    try:
        snap = BUS.snapshot()
        job = next((j for j in snap.get("jobs", []) if j.get("id") == job_id), None)
        if not job:
            return "other"
        if job.get("fail_kind"):
            return str(job["fail_kind"])
        blob = f"{job.get('error') or ''} {job.get('detail') or ''} {job.get('step') or ''}"
        if any(
            k in blob
            for k in (
                "人机",
                "验证码",
                "captcha",
                "FunCaptcha",
                "异常活动",
                "两波",
                "按压",
                "刷新后无法重新进入",
            )
        ):
            return "captcha"
        if any(k in blob for k in ("填表", "primaryButton", "表单", "Timeout")):
            return "form"
        if "OAuth" in blob or "oauth" in blob:
            return "oauth"
    except Exception:
        pass
    return "other"


def process_single_flow(controller):
    """跑一个号。返回 (ok: bool, fail_kind: str)。"""
    page = None
    job_id = None
    email = random_email()
    password = generate_strong_password()
    fail_kind = "other"

    try:
        if should_stop():
            return False, "other"

        job_id = BUS.create_job(email, password)
        BUS.step(job_id, "browser", "启动浏览器 / 创建上下文（有头可见）")
        page = controller.get_thread_page()
        try:
            focus_browser_window(page)
        except Exception:
            pass

        if should_stop():
            BUS.fail(job_id, "用户点击停止")
            return False, "other"

        result = controller.outlook_register(page, email, password, job_id=job_id)

        if not result:
            fail_kind = _infer_fail_kind(job_id)
            try:
                BUS.set_fail_kind(job_id, fail_kind)
            except Exception:
                pass
            BUS.fail(job_id, "注册失败（见上方步骤详情）")
            return False, fail_kind

        # 注册成功：若未开 OAuth2，直接以邮箱密码导出
        if not controller.enable_oauth2:
            line = f"{email}{controller.email_suffix}: {password}"
            export_line = line
            try:
                snap = BUS.snapshot()
                job = next((j for j in snap.get("jobs", []) if j.get("id") == job_id), None)
                if job and job.get("account_line"):
                    export_line = job["account_line"]
                    line = export_line
            except Exception:
                pass
            try:
                from export_accounts import append_export

                if "----" not in str(export_line):
                    export_line = append_export(
                        f"{email}{controller.email_suffix}", password
                    )
                BUS.set_account_line(job_id, export_line)
                BUS.succeed(
                    job_id,
                    f"已登录并导出 {export_line.split('----')[0]} -> 导出/export_accounts.txt",
                )
            except Exception:
                BUS.set_account_line(job_id, line)
                BUS.succeed(job_id, f"已登录并保存 {line}")
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return True, ""

        # OAuth2
        BUS.step(job_id, "oauth2", "开始 OAuth2 获取 refresh_token")
        token_result = get_access_token(page, email, password=password)
        full_email = f"{email}{controller.email_suffix}"
        if token_result[0]:
            refresh_token, access_token, expire_at = token_result
            try:
                with open(
                    os.path.join(os.path.dirname(__file__), "Results", "outlook_token.txt"),
                    "a",
                    encoding="utf-8",
                ) as f2:
                    f2.write(
                        f"{full_email}---{password}---{refresh_token}---{access_token}---{expire_at}\n"
                    )
            except Exception as _we:
                print(f"[Export] outlook_token.txt 写入失败: {_we}")
            try:
                from export_accounts import append_export, _load_client_id

                export_line = append_export(
                    full_email, password, _load_client_id(), refresh_token or ""
                )
            except Exception as _ex:
                export_line = f"{full_email}----{password}--------{refresh_token or ''}"
                print(f"[Export] warn: {_ex}")
            print(f"[Success: TokenAuth] - {full_email}")
            BUS.set_account_line(job_id, export_line)
            tok_preview = (refresh_token or "")[:18] + "..." if refresh_token else ""
            BUS.succeed(
                job_id,
                f"OAuth2 成功并已导出 {full_email} token={tok_preview} -> 导出/export_accounts.txt",
            )
            return True, ""

        try:
            from export_accounts import append_export

            export_line = append_export(full_email, password)
            BUS.set_account_line(job_id, export_line)
        except Exception:
            pass
        BUS.set_fail_kind(job_id, "oauth")
        BUS.fail(job_id, "OAuth2 获取失败（邮箱可能已创建，密码已尽量导出，Token 为空）")
        return False, "oauth"

    except Exception as e:
        print(e)
        if job_id:
            BUS.fail(job_id, f"异常中断: {type(e).__name__}: {e}")
        return False, "other"

    finally:
        controller.clean_up(page, "done_browser")


def _maybe_rotate_after_captcha_streak(
    captcha_fail_streak: int,
    threshold: int,
    max_rotate: int,
    use_dynamic: bool,
    auto_rotate: bool = False,
) -> int:
    """人机连败达阈值则换 IP（默认关闭：平台自行换出口，本地不改 sid）。"""
    # threshold<=0 或 auto_rotate=False → 永不自动改 sid
    if not auto_rotate or threshold <= 0:
        return 0 if captcha_fail_streak > 0 and not auto_rotate else captcha_fail_streak
    if captcha_fail_streak < threshold:
        return captcha_fail_streak
    if not use_dynamic:
        print("[IP] 人机连续失败达阈值，但当前非动态IP模式，跳过换出口")
        return 0
    print(
        f"[IP] 人机连续失败 {captcha_fail_streak} 次 ≥ {threshold}，开始换动态出口…"
    )
    BUS.set_run_message(f"人机连败 {captcha_fail_streak} 次，正在换出口并探测…")
    try:
        from start_dyn_proxy import rotate_and_probe

        result = rotate_and_probe(max_attempts=max_rotate)
        if result.get("ok"):
            msg = (
                f"已换出口 ip={result.get('ip')}"
                + ("（已回退最初会话）" if result.get("restored") else "")
            )
            print(f"[IP] {msg}")
            BUS.set_run_message(msg)
        else:
            msg = f"换出口后探测仍失败: {result.get('error')}"
            print(f"[IP] {msg}")
            BUS.set_run_message(msg)
    except Exception as e:
        print(f"[IP] 换出口异常: {type(e).__name__}: {e}")
        BUS.set_run_message(f"换出口异常: {e}")
    return 0


def run_concurrent_flows(
    controller,
    concurrent_flows=10,
    max_tasks=100,
    task_interval_sec: float = 20.0,
    captcha_fail_rotate_threshold: int = 0,
    dyn_rotate_max_attempts: int = 4,
    use_dynamic: bool = False,
    auto_rotate_dyn_sid: bool = False,
):
    """
    数量跑满：不因连续失败整轮停。
    号间间隔 = task_interval_sec（来自反检测等待）。
    默认不自动改动态 sid（平台自行换出口）；仅 auto_rotate_dyn_sid=true 且阈值>0 时才轮换。
    """
    task_counter = 0
    succeeded_tasks = 0
    failed_tasks = 0
    captcha_fail_streak = 0
    user_stopped = False

    interval = max(0.0, float(task_interval_sec or 0))
    # 0 = 关闭自动换 sid
    threshold = int(captcha_fail_rotate_threshold or 0)
    max_rotate = max(1, min(int(dyn_rotate_max_attempts or 4), 5))
    auto_rotate = bool(auto_rotate_dyn_sid) and threshold > 0

    with ThreadPoolExecutor(max_workers=concurrent_flows) as executor:
        running_futures = set()
        last_finish_at = 0.0

        while task_counter < max_tasks or len(running_futures) > 0:
            if should_stop() and not user_stopped:
                user_stopped = True
                BUS.set_run_message("用户请求停止，不再提交新任务")
                print("[Abort] 用户停止")

            done_futures = {f for f in running_futures if f.done()}
            for future in done_futures:
                ok = False
                kind = "other"
                try:
                    result = future.result()
                    if isinstance(result, tuple):
                        ok, kind = bool(result[0]), (result[1] or "other")
                    else:
                        ok = bool(result)
                        kind = "" if ok else "other"
                except Exception as e:
                    ok = False
                    kind = "other"
                    print(e)

                if ok:
                    succeeded_tasks += 1
                    captcha_fail_streak = 0
                else:
                    failed_tasks += 1
                    if kind == "captcha":
                        captcha_fail_streak += 1
                        if auto_rotate:
                            print(
                                f"[IP] 人机失败计数 streak={captcha_fail_streak}/{threshold}"
                            )
                            captcha_fail_streak = _maybe_rotate_after_captcha_streak(
                                captcha_fail_streak,
                                threshold,
                                max_rotate,
                                use_dynamic,
                                auto_rotate=True,
                            )
                        else:
                            # 平台自行换出口，本地不改 sid
                            if captcha_fail_streak == 1 or captcha_fail_streak % 3 == 0:
                                print(
                                    f"[IP] 人机失败 x{captcha_fail_streak}（自动换sid已关闭，跟平台动态出口）"
                                )

                last_finish_at = time.time()
                running_futures.remove(future)
                BUS.set_run_message(
                    f"进度 {task_counter}/{max_tasks} · 成功 {succeeded_tasks} · 失败 {failed_tasks}"
                    + (
                        f" · 人机连败 {captcha_fail_streak}"
                        if captcha_fail_streak and auto_rotate
                        else ""
                    )
                )

            # 提交新任务：数量未满 + 未停止 + 有空槽
            # 号间间隔：上一号结束后等 interval 秒（并发=1 时最明显）
            can_submit = (
                (not user_stopped)
                and task_counter < max_tasks
                and not should_stop()
            )
            while can_submit and len(running_futures) < concurrent_flows:
                if last_finish_at > 0 and interval > 0:
                    waited = time.time() - last_finish_at
                    if waited < interval:
                        # 有正在跑的就不在这里死等；无在跑则睡到间隔够
                        if len(running_futures) == 0:
                            sleep_for = interval - waited
                            print(
                                f"[Wait] 号间间隔 {interval:.0f}s（反检测），剩余 {sleep_for:.1f}s"
                            )
                            BUS.set_run_message(
                                f"号间等待 {sleep_for:.0f}s（反检测）后继续…"
                            )
                            # 可被停止打断
                            end = time.time() + sleep_for
                            while time.time() < end:
                                if should_stop():
                                    break
                                time.sleep(min(0.5, end - time.time()))
                            if should_stop():
                                break
                        else:
                            break  # 等正在跑的结束再计间隔

                if should_stop() or user_stopped:
                    break
                new_future = executor.submit(process_single_flow, controller)
                running_futures.add(new_future)
                task_counter += 1
                print(f"已提交 {task_counter}/{max_tasks} 任务.")
                BUS.set_run_message(
                    f"已提交 {task_counter}/{max_tasks} · 成功 {succeeded_tasks} · 失败 {failed_tasks}"
                )
                # 同一 tick 多并发时，首发后不再强制间隔（由各任务自己结束计时）
                if concurrent_flows > 1 and len(running_futures) < concurrent_flows:
                    last_finish_at = 0.0  # 允许立刻填满并发槽
                can_submit = (
                    (not user_stopped)
                    and task_counter < max_tasks
                    and not should_stop()
                    and len(running_futures) < concurrent_flows
                )
                if concurrent_flows == 1:
                    break  # 串行：一次只丢一个

            time.sleep(0.5)

    print(
        f"\n[Result] - 共: {task_counter}, 成功 {succeeded_tasks}, 失败 {failed_tasks}"
        + (" (用户停止)" if user_stopped else "")
    )
    BUS.finish_run(
        f"完成：共 {task_counter}，成功 {succeeded_tasks}，失败 {failed_tasks}"
        + ("（用户停止）" if user_stopped else "（已跑满数量）")
    )
    return {
        "submitted": task_counter,
        "succeeded": succeeded_tasks,
        "failed": failed_tasks,
        "stopped_early": user_stopped,
    }


def run_from_config(config_path: str = "config.json"):
    """供 dashboard / CLI 共用的入口。"""
    show_banner()
    clear_global_stop()
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    os.makedirs("Results", exist_ok=True)

    try:
        from config_store import save_config, effective_proxy

        data = save_config(data)
        proxy_desc = effective_proxy(data) or "本机直连(机房流量)"
    except Exception:
        proxy_desc = data.get("proxy") or "本机直连"

    max_tasks = int(data["max_tasks"])
    concurrent_flows = int(data["concurrent_flows"])
    use_res = bool(data.get("use_residential", False))
    use_dyn = bool(data.get("use_dynamic", False))
    # 号间间隔 = 反检测等待
    task_interval = float(data.get("bot_protection_wait") or 20)
    # 默认关闭自动改 sid（0 / auto_rotate_dyn_sid=false）
    auto_rotate = bool(data.get("auto_rotate_dyn_sid", False))
    captcha_thresh = int(data.get("captcha_fail_rotate_threshold") or 0)
    if not auto_rotate:
        captcha_thresh = 0
    dyn_max_rot = int(data.get("dyn_rotate_max_attempts") or 4)

    if use_dyn:
        try:
            from start_dyn_proxy import start as start_dyn, set_dynamic_proxy_url

            # 固定使用 config 里的 dynamic_proxy，绝不在启动时 rotate
            raw = (data.get("dynamic_proxy") or "").strip()
            if raw:
                set_dynamic_proxy_url(raw)
            start_dyn(force=True, rotate=False)
        except Exception as _de:
            print(f"[Net] 动态IP桥启动警告: {_de}")

    incog = bool(data.get("incognito", True))
    mode = "动态IP" if use_dyn else ("住宅IP" if use_res else "本机直连")
    print(f"[Net] 流量模式: {mode} | 实际代理: {proxy_desc}")
    print(
        f"[Run] 数量={max_tasks} 并发={concurrent_flows} 号间间隔={task_interval:.0f}s(反检测) "
        f"自动换sid={'开' if auto_rotate and captcha_thresh > 0 else '关(跟平台)'}"
    )
    print(f"[UI] 有头=是 | 无痕={'开' if incog else '关'}")
    print("[UI] 请留意任务栏 Chromium 新窗口")
    BUS.reset_run(
        max_tasks=max_tasks,
        concurrent=concurrent_flows,
        use_residential=use_res or use_dyn,
    )

    if data["choose_browser"] == "patchright":
        selected_controller = PatchrightController()
    elif data["choose_browser"] == "playwright":
        selected_controller = PlaywrightController()
    else:
        print("不支持的浏览器类型，填写patchright或者playwright")
        BUS.finish_run("浏览器类型配置错误")
        return None

    try:
        return run_concurrent_flows(
            selected_controller,
            concurrent_flows,
            max_tasks,
            task_interval_sec=task_interval,
            captcha_fail_rotate_threshold=captcha_thresh,
            dyn_rotate_max_attempts=dyn_max_rot,
            use_dynamic=use_dyn,
            auto_rotate_dyn_sid=auto_rotate,
        )
    finally:
        selected_controller.clean_up(type="all_browser")


if __name__ == "__main__":
    run_from_config()
