from __future__ import annotations

import os
import shutil
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .config import expand


def send_daily_screenshots(root: Path, config: dict[str, Any], selected: list[str],
                           completed: list[dict[str, str]], started_epoch: float) -> tuple[bool, str]:
    settings = config.get("email_report", {})
    screenshot_map = settings.get("screenshots", {})
    sent_by_steps = {
        str(name) for name in settings.get("workflow_step_sends", [])
    }
    if not settings.get("enabled", False) and not screenshot_map:
        return True, "邮件报告未启用"
    files: list[tuple[str, Path]] = []
    for workflow in selected:
        if workflow in sent_by_steps:
            continue
        raw = screenshot_map.get(workflow)
        if not raw:
            continue
        path = Path(expand(str(raw)))
        try:
            if path.is_file() and path.stat().st_mtime >= started_epoch - 5:
                files.append((workflow, path))
        except OSError:
            continue
    if not files:
        if selected and all(workflow in sent_by_steps for workflow in selected):
            return True, "本批截图已由各工作流结束步骤直接发送"
        return False, "本批每日流程没有产生可发送的最新游戏截图"

    try:
        batch_time = datetime.fromtimestamp(started_epoch).astimezone()
    except (OSError, OverflowError, ValueError):
        batch_time = datetime.now().astimezone()
    evidence_dir = root / "logs" / "daily_evidence" / f"{batch_time:%Y-%m-%d}" / f"{batch_time:%H%M%S}"
    status_by_workflow = {item["workflow"]: item.get("status", "unknown") for item in completed}
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for workflow, path in files:
        status = status_by_workflow.get(workflow, "unknown")
        try:
            shutil.copy2(path, evidence_dir / f"{workflow}_{status}{path.suffix.lower()}")
        except OSError:
            continue

    if not settings.get("enabled", False):
        return True, f"本批截图已保存到 {evidence_dir}；邮件报告未启用"

    attachments: list[tuple[str, bytes, str]] = []
    for workflow, path in files:
        packed = False
        try:
            import cv2
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is not None:
                max_width = int(settings.get("image_max_width", 1280))
                if image.shape[1] > max_width:
                    scale = max_width / image.shape[1]
                    image = cv2.resize(image, (max_width, round(image.shape[0] * scale)),
                                       interpolation=cv2.INTER_AREA)
                quality = int(settings.get("jpeg_quality", 70))
                ok, encoded = cv2.imencode(".jpg", image,
                                           [cv2.IMWRITE_JPEG_QUALITY, quality])
                if ok:
                    attachments.append((f"{workflow}.jpg", encoded.tobytes(), "jpeg"))
                    packed = True
        except ImportError:
            pass
        if not packed:
            subtype = "png" if path.suffix.casefold() == ".png" else "jpeg"
            attachments.append((f"{workflow}{path.suffix}", path.read_bytes(), subtype))

    user = os.environ.get(str(settings.get("user_env", "GAMEFLOW_SMTP_USER")), "").strip()
    password = os.environ.get(str(settings.get("password_env", "GAMEFLOW_SMTP_AUTH_CODE")), "").strip()
    if not user or not password:
        return False, f"本批截图已保存到 {evidence_dir}；尚未设置发件邮箱或 SMTP 授权码，图片未发送"

    recipient = str(settings.get("recipient", "")).strip()
    if not recipient:
        return False, "邮件收件地址为空"
    labels = {name: wf.get("display_name", name) for name, wf in config.get("workflows", {}).items()}
    status_lines = []
    for item in completed:
        if item["workflow"] not in selected:
            continue
        label = labels.get(item["workflow"], item["workflow"])
        if item.get("retried"):
            status_lines.append(
                f"{label}: 第一轮 {item.get('first_status', 'unknown')} → "
                f"第二轮 {item['status']}")
        else:
            status_lines.append(f"{label}: 第一轮 {item['status']}（无需重试）")
    message = EmailMessage()
    message["From"] = user
    message["To"] = recipient
    message["Subject"] = f"每日游戏流程截图 {datetime.now().astimezone():%Y-%m-%d %H:%M}"
    message.set_content("每日游戏流程及失败任务自动重试已结束。\n\n" +
                        "\n".join(status_lines) +
                        "\n\n附件为两轮结束后保留的最终游戏内截图。")
    for filename, content, subtype in attachments:
        message.add_attachment(content, maintype="image", subtype=subtype, filename=filename)
    host = str(settings.get("smtp_host", "smtp.qq.com"))
    port = int(settings.get("smtp_port", 465))
    security = str(settings.get("smtp_security", "ssl")).casefold()
    timeout = float(settings.get("smtp_timeout", 120))
    attempts = max(1, int(settings.get("smtp_attempts", 3)))
    last_error = None
    for attempt in range(1, attempts + 1):
        smtp = None
        try:
            if security == "starttls":
                smtp = smtplib.SMTP(host, port, timeout=timeout)
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP_SSL(host, port, timeout=timeout)
            smtp.login(user, password)
            smtp.send_message(message)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(float(settings.get("smtp_retry_delay", 5)))
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:
                    try:
                        smtp.close()
                    except Exception:
                        pass
    if last_error is not None:
        return False, f"邮件重试 {attempts} 次后仍发送失败：{last_error}"
    return True, (f"本批截图已保存到 {evidence_dir}；"
                  f"已将 {len(attachments)} 张游戏截图直接发送到 {recipient}")
