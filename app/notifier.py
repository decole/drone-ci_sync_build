import asyncio
import logging
import random
import smtplib
from email.message import EmailMessage
from typing import Tuple, List, Optional
import httpx

from app.database import SessionLocal
from app.models import NotificationSettings, get_notification_settings
from app.poller import get_http_client
from app.metrics import NOTIFICATIONS_SENT_TOTAL

logger = logging.getLogger("drone_sync.notifier")


# --- Channel Specific Senders ---

async def send_telegram(bot_token: str, chat_id: str, message: str) -> Tuple[bool, str]:
    """Send message via Telegram Bot API."""
    if not bot_token or not chat_id:
        return False, "Telegram Bot Token или Chat ID не настроены"

    url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
    payload = {
        "chat_id": chat_id.strip(),
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    client = get_http_client()
    try:
        resp = await client.post(url, json=payload, timeout=10.0)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            NOTIFICATIONS_SENT_TOTAL.labels(channel="telegram", status="success").inc()
            return True, "Сообщение в Telegram успешно отправлено"
        else:
            desc = data.get("description", resp.text[:150])
            NOTIFICATIONS_SENT_TOTAL.labels(channel="telegram", status="error").inc()
            return False, f"Ошибка Telegram API: {desc}"
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)
        NOTIFICATIONS_SENT_TOTAL.labels(channel="telegram", status="error").inc()
        return False, f"Сетевая ошибка Telegram: {str(e)}"


async def send_vk(group_token: str, peer_id: str, message: str) -> Tuple[bool, str]:
    """Send message to VK User/Chat/Group via VK messages.send API."""
    if not group_token or not peer_id:
        return False, "VK Access Token или Peer ID не настроены"

    url = "https://api.vk.com/method/messages.send"
    data = {
        "peer_id": peer_id.strip(),
        "message": message,
        "random_id": random.randint(1, 2147483647),
        "v": "5.199",
        "access_token": group_token.strip()
    }

    client = get_http_client()
    try:
        resp = await client.post(url, data=data, timeout=10.0)
        res_data = resp.json()
        if "response" in res_data:
            NOTIFICATIONS_SENT_TOTAL.labels(channel="vk", status="success").inc()
            return True, "Сообщение в ВК успешно отправлено"
        elif "error" in res_data:
            err_msg = res_data["error"].get("error_msg", "Unknown error")
            NOTIFICATIONS_SENT_TOTAL.labels(channel="vk", status="error").inc()
            return False, f"Ошибка VK API: {err_msg}"
        else:
            NOTIFICATIONS_SENT_TOTAL.labels(channel="vk", status="error").inc()
            return False, f"Непредвиденный ответ VK: {resp.text[:150]}"
    except Exception as e:
        logger.error("Failed to send VK message: %s", e)
        NOTIFICATIONS_SENT_TOTAL.labels(channel="vk", status="error").inc()
        return False, f"Сетевая ошибка VK: {str(e)}"


def _send_smtp_sync(
    host: str,
    port: int,
    username: Optional[str],
    password: Optional[str],
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body: str,
    use_tls: bool = True
) -> Tuple[bool, str]:
    """Synchronous SMTP worker executed in thread pool."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr or (username or "drone-sync@localhost")
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=12) as server:
                if use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        NOTIFICATIONS_SENT_TOTAL.labels(channel="email", status="success").inc()
        return True, f"Email успешно отправлен на: {', '.join(to_addrs)}"
    except Exception as e:
        logger.error("SMTP sending error: %s", e)
        NOTIFICATIONS_SENT_TOTAL.labels(channel="email", status="error").inc()
        return False, f"Ошибка отправки Email: {str(e)}"


async def send_email(
    host: str,
    port: int,
    username: Optional[str],
    password: Optional[str],
    from_addr: str,
    to_addrs: List[str],
    subject: str,
    body: str,
    use_tls: bool = True
) -> Tuple[bool, str]:
    """Send email asynchronously using thread executor."""
    if not host or not to_addrs:
        return False, "SMTP Host или адреса получателей не указаны"

    return await asyncio.to_thread(
        _send_smtp_sync,
        host=host.strip(),
        port=port or 587,
        username=username.strip() if username else None,
        password=password,
        from_addr=from_addr.strip() if from_addr else (username or "drone-ci-sync@localhost"),
        to_addrs=[t.strip() for t in to_addrs if t.strip()],
        subject=subject,
        body=body,
        use_tls=use_tls
    )


# --- Dispatcher for Poller Events ---

async def dispatch_event_notification(
    event_type: str,  # "build" or "error"
    title: str,
    message: str,
    repo_name: str = "",
    branch: str = "",
    commit_sha: str = "",
    build_number: Optional[int] = None,
    db: Optional[SessionLocal] = None
):
    """
    Background dispatcher checking active notification channels
    and broadcasting the event.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True
    try:
        cfg = get_notification_settings(db)

        # Check event trigger filter
        if event_type == "build" and not cfg.notify_on_build:
            return
        if event_type == "error" and not cfg.notify_on_error:
            return

        plain_tg_token = cfg.get_plain_telegram_bot_token()
        plain_vk_token = cfg.get_plain_vk_group_token()
        plain_smtp_pw = cfg.get_plain_smtp_password()

        tasks = []

        # 1. Telegram
        if cfg.telegram_enabled and plain_tg_token and cfg.telegram_chat_id:
            icon = "🚀" if event_type == "build" else "⚠️"
            tg_text = f"<b>{icon} {title}</b>\n\n"
            if repo_name:
                tg_text += f"📦 <b>Репозиторий:</b> {repo_name}\n"
            if branch:
                tg_text += f"🌿 <b>Ветка:</b> <code>{branch}</code>\n"
            if commit_sha:
                tg_text += f"🔖 <b>SHA:</b> <code>{commit_sha[:7]}</code>\n"
            if build_number:
                tg_text += f"🔨 <b>Drone Build:</b> #{build_number}\n"
            tg_text += f"\nℹ️ {message}"

            tasks.append(send_telegram(plain_tg_token, cfg.telegram_chat_id, tg_text))

        # 2. VK
        if cfg.vk_enabled and plain_vk_token and cfg.vk_peer_id:
            icon = "🚀" if event_type == "build" else "⚠️"
            vk_text = f"{icon} {title}\n\n"
            if repo_name:
                vk_text += f"📦 Репозиторий: {repo_name}\n"
            if branch:
                vk_text += f"🌿 Ветка: {branch}\n"
            if commit_sha:
                vk_text += f"🔖 SHA: {commit_sha[:7]}\n"
            if build_number:
                vk_text += f"🔨 Drone Build: #{build_number}\n"
            vk_text += f"\nℹ️ {message}"

            tasks.append(send_vk(plain_vk_token, cfg.vk_peer_id, vk_text))

        # 3. Email
        if cfg.email_enabled and cfg.smtp_host and cfg.smtp_to:
            recipients = [e.strip() for e in cfg.smtp_to.replace("\n", ",").split(",") if e.strip()]
            if recipients:
                mail_subject = f"[Drone Sync] {title}"
                mail_body = f"{title}\n\n"
                if repo_name:
                    mail_body += f"Репозиторий: {repo_name}\n"
                if branch:
                    mail_body += f"Ветка: {branch}\n"
                if commit_sha:
                    mail_body += f"Commit SHA: {commit_sha}\n"
                if build_number:
                    mail_body += f"Drone CI Build: #{build_number}\n"
                mail_body += f"\nСообщение:\n{message}\n"

                tasks.append(send_email(
                    host=cfg.smtp_host,
                    port=cfg.smtp_port or 587,
                    username=cfg.smtp_username,
                    password=plain_smtp_pw,
                    from_addr=cfg.smtp_from or "",
                    to_addrs=recipients,
                    subject=mail_subject,
                    body=mail_body,
                    use_tls=cfg.smtp_use_tls
                ))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.error("Notification task exception: %s", r)

    except Exception as e:
        logger.error("Error in dispatch_event_notification: %s", e)
    finally:
        if close_db:
            db.close()
