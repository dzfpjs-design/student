from __future__ import annotations

"""Truthful notification adapters.

Adapters deliberately have no demo/fallback transport: a successful result means the
configured provider accepted the message.  It is still not proof a human opened it.
"""

from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar
from email.message import EmailMessage
from email.utils import make_msgid
import json
import smtplib
import ssl
from typing import Protocol

import httpx


class ChannelDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = True, *, delivery_unknown: bool = False):
        super().__init__(message)
        self.code = code
        self.delivery_unknown = delivery_unknown
        self.retryable = retryable and not delivery_unknown


class ChannelSubmissionCancelled(ChannelDeliveryError):
    """The channel has not submitted any report body to the provider."""
    def __init__(self, reason: str):
        super().__init__('SUBMISSION_CANCELLED', reason, False)
        self.reason = reason


def _admit_external_delivery_budget():
    """This original local check runs before any transport side effect."""
    from .money_egress import BudgetHold, deny_unpriced
    try:
        deny_unpriced('notification.external_delivery')
    except BudgetHold:
        # Do not expose arbitrary BudgetHold text or classify later adapter errors.
        raise ChannelDeliveryError(
            'NOTIFICATION_BUDGET_ADMISSION_BLOCKED',
            '费用控制尚未准入，本次通知尚未开始发送。', False,
            delivery_unknown=False) from None


_submission_check = ContextVar('report_channel_submission_check', default=None)


@contextmanager
def submission_guard(check):
    """Bind one synchronous adapter call; never share a callback across workers."""
    token = _submission_check.set(check)
    try:
        yield
    finally:
        _submission_check.reset(token)


def _check_submission():
    check = _submission_check.get()
    if check is not None:
        reason = check()
        if reason:
            raise ChannelSubmissionCancelled(reason)


@dataclass(frozen=True)
class ChannelReceipt:
    provider_message_id: str | None
    accepted_at: str | None = None
    client_message_id: str | None = None


MAX_EMAIL_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES = 12 * 1024 * 1024
MAX_EMAIL_ATTACHMENTS = 3


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content: bytes = field(repr=False)
    content_type: str = "application/zip"


def validate_email_attachments(attachments: tuple[EmailAttachment, ...]) -> tuple[EmailAttachment, ...]:
    # No path, URL, arbitrary MIME header or lazy file reader is an attachment capability.
    if type(attachments) is not tuple or len(attachments) > MAX_EMAIL_ATTACHMENTS:
        raise ChannelDeliveryError("SMTP_ATTACHMENT_INVALID", "邮件附件集合无效。", False)
    total = 0
    for item in attachments:
        if type(item) is not EmailAttachment:
            raise ChannelDeliveryError("SMTP_ATTACHMENT_INVALID", "邮件附件类型无效。", False)
        name = item.filename
        if (type(name) is not str or not 1 <= len(name) <= 120
                or any(ord(ch) < 32 or ord(ch) == 127 or ch in '/\\:' for ch in name)
                or name.startswith('.') or not name.endswith('.zip')
                or item.content_type != 'application/zip' or type(item.content) is not bytes):
            raise ChannelDeliveryError("SMTP_ATTACHMENT_INVALID", "邮件附件元数据无效。", False)
        if not 0 < len(item.content) <= MAX_EMAIL_ATTACHMENT_BYTES:
            raise ChannelDeliveryError("SMTP_ATTACHMENT_TOO_LARGE", "邮件附件大小超过安全边界。", False)
        total += len(item.content)
    if total > MAX_EMAIL_ATTACHMENTS_TOTAL_BYTES:
        raise ChannelDeliveryError("SMTP_ATTACHMENT_TOO_LARGE", "邮件附件总大小超过安全边界。", False)
    return attachments


class NotificationChannel(Protocol):
    channel: str

    def send(self, recipient: str, subject: str, text: str, html: str, config: dict) -> ChannelReceipt: ...


class SmtpChannel:
    channel = "email"

    def send(self, recipient: str, subject: str, text: str, html: str, config: dict, *, attachments: tuple[EmailAttachment, ...] = ()) -> ChannelReceipt:
        attachments = validate_email_attachments(attachments)
        _admit_external_delivery_budget()
        host = str(config.get("host") or "").strip()
        username = str(config.get("username") or "").strip()
        password = str(config.get("password") or "")
        sender = str(config.get("from_address") or username).strip()
        port = int(config.get("port") or (465 if config.get("use_ssl") else 587))
        if not host or not sender:
            raise ChannelDeliveryError("SMTP_CONFIG_INVALID", "SMTP 主机和发件人地址是必填项。", False)
        if not config.get("allow_external_delivery", False):
            raise ChannelDeliveryError("EXTERNAL_DELIVERY_NOT_AUTHORIZED", "尚未授权向外部邮件服务发送孩子数据。", False)
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message["Message-ID"] = make_msgid(domain="home-study.local")
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        # The attachment gives recipients a portable, offline-readable copy
        # without exposing a local-device URL that a mail recipient cannot open.
        message.add_attachment(
            html.encode("utf-8"), maintype="text", subtype="html",
            filename="学习成长报告.html",
        )
        for attachment in attachments:
            message.add_attachment(attachment.content, maintype="application", subtype="zip",
                                   filename=attachment.filename)
        client = None
        submitting = False
        try:
            if config.get("use_ssl"):
                client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=15)
                if config.get("starttls", True):
                    client.starttls(context=ssl.create_default_context())
            if username:
                client.login(username, password)
            _check_submission()
            submitting = True
            refused = client.send_message(message)
            if refused:
                raise ChannelDeliveryError("SMTP_RECIPIENT_REFUSED", "邮件服务器拒绝该收件人。", False)
            return ChannelReceipt(provider_message_id=None, client_message_id=message["Message-ID"])
        except ChannelDeliveryError:
            raise
        except smtplib.SMTPAuthenticationError as error:
            raise ChannelDeliveryError("SMTP_CREDENTIALS_INVALID", "SMTP 凭证被拒绝。", False) from error
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as error:
            # An explicit negative SMTP response is distinct from a lost DATA acknowledgement.
            code = getattr(error, "smtp_code", 550)
            raise ChannelDeliveryError("SMTP_SUBMISSION_REJECTED", "邮件服务器明确拒绝了提交。", 400 <= code < 500) from error
        except (OSError, smtplib.SMTPException) as error:
            raise ChannelDeliveryError("SMTP_DELIVERY_UNKNOWN" if submitting else "SMTP_UNAVAILABLE",
                                       "提交后未获得可靠回执，请核对后决定是否重发。" if submitting else "无法连接 SMTP 服务器。",
                                       True, delivery_unknown=submitting) from error
        finally:
            if client is not None:
                # QUIT is housekeeping, not the acceptance acknowledgement for DATA.
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    pass
                finally:
                    client.close()


class WeComBotChannel:
    """Enterprise WeChat group-bot webhook.

    This is intentionally *not* a personal-WeChat adapter. The webhook must be
    provisioned by an organisation and is only used after explicit family consent.
    """

    channel = "wechat_work_bot"

    def send(self, recipient: str, subject: str, text: str, html: str, config: dict) -> ChannelReceipt:
        _admit_external_delivery_budget()
        webhook = str(config.get("webhook_url") or "").strip()
        if not webhook.startswith("https://"):
            raise ChannelDeliveryError("WECOM_WEBHOOK_INVALID", "企业微信群机器人 Webhook 必须是 HTTPS 地址。", False)
        if not config.get("allow_external_delivery", False):
            raise ChannelDeliveryError("EXTERNAL_DELIVERY_NOT_AUTHORIZED", "尚未授权向企业微信发送孩子数据。", False)
        # The group webhook has no per-user address. `recipient` is a local consent
        # label, retained for audit and not sent to Tencent as an identifier.
        body = f"{subject}\n\n{text}"
        if len(body.encode("utf-8")) > 2048:
            raise ChannelDeliveryError(
                "WECOM_MESSAGE_TOO_LARGE",
                "企业微信群机器人文本超过 2048 UTF-8 bytes；请改用最小披露、邮件或在本机查看。",
                False,
            )
        try:
            _check_submission()
            response = httpx.post(webhook, json={"msgtype": "text", "text": {"content": body}}, timeout=15)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ChannelDeliveryError("WECOM_NETWORK_ERROR", "企业微信连接未建立。", True) from error
        except httpx.HTTPError as error:
            raise ChannelDeliveryError("WECOM_DELIVERY_UNKNOWN", "请求后未获得可靠回执，请核对后决定是否重发。", delivery_unknown=True) from error
        if response.status_code >= 500:
            raise ChannelDeliveryError("WECOM_DELIVERY_UNKNOWN", "服务器未返回可靠接受回执。", delivery_unknown=True)
        if response.status_code == 429:
            raise ChannelDeliveryError("WECOM_TEMPORARY_FAILURE", "企业微信暂时无法接收消息。", True)
        if response.status_code >= 400:
            raise ChannelDeliveryError("WECOM_REQUEST_REJECTED", "企业微信拒绝该消息配置。", False)
        try:
            payload = response.json()
        except ValueError as error:
            raise ChannelDeliveryError("WECOM_INVALID_RESPONSE", "企业微信返回了无效响应。", delivery_unknown=True) from error
        if not isinstance(payload, dict) or type(payload.get("errcode")) is not int or not 200 <= response.status_code < 300:
            raise ChannelDeliveryError("WECOM_INVALID_RESPONSE", "企业微信未返回可验证的接受结果。", delivery_unknown=True)
        if payload["errcode"] != 0:
            raise ChannelDeliveryError("WECOM_API_ERROR", f"企业微信错误码：{payload.get('errcode')}", payload.get("errcode") in {45009, 93000})
        return ChannelReceipt(provider_message_id=str(payload.get("msgid") or "") or None)


def channels() -> dict[str, NotificationChannel]:
    return {"email": SmtpChannel(), "wechat_work_bot": WeComBotChannel()}
