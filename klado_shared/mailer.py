"""
Verification email — plain SMTP, configured from the database (admin page) with
the environment as a fallback.

Why SMTP and not an API
-----------------------
The only mailbox the project has is an Aliyun one, and both Aliyun options speak
SMTP: **企业邮箱** (`smtp.qiye.aliyun.com:465`, needs a client-specific password for
the account) or **邮件推送 / DirectMail** (`smtpdm.aliyun.com:465`, needs a verified
sending domain + DNS records for it). 企业邮箱 is the one that needs no DNS work.

Config lives in `services/mail_config.py`: the admin page writes it to the
`mail_settings` table (password encrypted), and SMTP_HOST / SMTP_PORT / SMTP_USER /
SMTP_PASSWORD / SMTP_FROM / SMTP_SECURITY are still read as a per-field fallback for
deployments that set them. See that module for why this is not env-only.

⚠️ Delivery is not guaranteed, and that is expected. This module therefore reports
*what happened* (`sent` / `unconfigured` / `failed`) — plus, for an admin-initiated
test, the SMTP server's own error text — instead of pretending success. A code that
cannot be emailed is still valid, and the admin page lists live codes so it can be
relayed by hand.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from klado_shared.config import mail_asset_dirs, settings
from klado_shared.i18n import pick
from klado_shared import mail_config, mail_templates

_LOG = logging.getLogger(__name__)

# What the mailbox provider's error codes actually mean. Surfaced verbatim in the
# admin page's test result: a bare "failed" leaves the admin debugging blind.
_SMTP_HINTS = {
    526: "账号密码认证错误。邮件服务商后台最常见的是「三方客户端访问策略」黑名单还开着"
         "（关掉，或把账号加进例外，约 5 分钟生效）；其次是生成安全密码时没填设备名/没点确定，只复制了密码。"
         " / Wrong account or password. Usually the provider's third-party-client policy still blocks"
         " the account (turn it off, or add it to the exception list — about 5 minutes to take"
         " effect); less often an app-specific password was generated without a device name.",
    535: "认证失败：密码不对，或没有用「三方客户端安全密码」。"
         " / Authentication failed: wrong password, or the app-specific password was not used.",
    530: "服务器要求先认证：该账号的 SMTP 服务权限没放开。"
         " / The server demands authentication first: SMTP is not enabled for this account.",
    550: "被拒收：发信地址与登录账号不一致，或域名 SPF/DKIM 没配好。"
         " / Rejected: the From address does not match the login account, or SPF/DKIM is misconfigured.",
    554: "被拒收：内容/域名信誉问题，或免费版外发被限制。"
         " / Rejected: content or domain reputation, or outbound mail is capped on the free plan.",
    421: "发信频率或数量超限，稍后再试。"
         " / Sending rate or volume exceeded — try again later.",
}


def _banner_url(*filenames: str, base: str = "") -> str:
    """
    Absolute URL for an email banner, or "" when it has not been added yet.

    A banner is an asset *inside* the email: it has to load in whichever environment sent
    the mail, so the caller passes that environment's base. When no base is given we fall
    back to the directory of AGENT_SKILL_URL — and only when that value actually names a
    directory, so a bare `agent.zip` does not turn into a link to itself.

    ⚠️ The lookup goes through `mail_asset_dirs()` rather than the main app's
    `resolve_frontend_dir()`: this module is shared, and the console sends mail too. See
    that function for why the two questions are not the same question.
    """
    base = (base or "").rstrip("/")
    if not base:
        configured = (settings.AGENT_SKILL_URL or "").strip().rstrip("/")
        base = configured.rsplit("/", 1)[0] if "/" in configured else ""
    if not base:
        return ""
    for directory in mail_asset_dirs():
        for filename in filenames:
            if os.path.isfile(os.path.join(directory, filename)):
                return f"{base}/{filename}"
    return ""


def configured() -> bool:
    return mail_config.is_configured()


def _app_url(asset_base: str) -> str:
    """
    The app's own address as it goes into a mail, always with a trailing slash.

    Derived from the sending environment (same rule as the banners, and for the same
    reason: a link has to land where the mail came from), never from the repository.
    """
    base = (asset_base or "").rstrip("/")
    return base + "/" if base else ""


def _build_message(to_email: str, code: str, sender: str,
                   asset_base: str = "") -> EmailMessage:
    """
    HTML + plain-text alternative; see services/mail_templates.py.

    ⚠️ `asset_base` has to be threaded through from the caller. It used to be read as
    a bare name here, which is a NameError the moment mail is actually configured —
    i.e. every `register/start` on an environment with SMTP returned 500 and the
    verification code was never sent. It stayed invisible because the unconfigured
    path returns before this is reached, and the local suites registered through
    `/register/complete` without asserting on `/register/start`.
    """
    minutes = max(1, settings.AUTH_CODE_TTL_SECONDS // 60)
    return _build_message_from(
        mail_templates.verification_code_email(
            code, minutes, to_email,
            _banner_url("email-banner-verification.jpg", "email-banner-verification.png",
                        base=asset_base),
            app_url=_app_url(asset_base)),
        to_email, sender)


def _build_message_from(template: tuple[str, str, str], to_email: str,
                        sender: str) -> EmailMessage:
    """
    (subject, html, text) → multipart/alternative message.

    The extra headers are spam-filter bait reduction, not decoration. The app sends
    transactional mail from a small custom domain (`klado.team`) into corporate Outlook
    inboxes — exactly the profile that lands in junk — so every signal a filter can read
    for "this is a real product, not bulk mail" is set deliberately:

    * `Date`/`Message-ID`: otherwise Python generates them anyway, but setting them here
      keeps the header order stable and the value deterministic per test;
    * `Auto-Submitted: auto-generated` + `X-Auto-Response-Suppress`: this is generated
      mail. Outlook/VBSS honours the second and skips out-of-office replies and
      auto-responses back at us (which would otherwise bounce and dirty the sender's
      reputation);
    * `List-Id`: a stable, well-formed List-Id is one of the few positive signals a
      bulk-ish classifier reads, and it gives recipients' filters something consistent
      to learn "not junk" on.
    """
    subject, html, text = template
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{mail_templates.BRAND} <{sender}>"
    msg["To"] = to_email
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = ("DR, RN, NRN, OOF, AutoReply")
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "localhost"
    msg["List-Id"] = f"{mail_templates.BRAND} notifications <notify.{domain}>"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _explain(exc: Exception, lang: str | None = None) -> str:
    """The SMTP server's own words *plus* what they mean — a bare "failed" leaves whoever
    has to fix it blind, and `526 Authentication failure` decides whether the fix is in the
    mail console (the third-party-client policy) or in the password."""
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(getattr(exc, "smtp_code", 0) or 0)
        label = ("认证失败 / authentication failed" if isinstance(exc, smtplib.SMTPAuthenticationError)
                 else "服务器拒绝 / rejected by the server")
        return (f"{pick(label, lang)}（{code}）：{exc.smtp_error!r}"
                + (f"\n{pick(_SMTP_HINTS[code], lang)}" if code in _SMTP_HINTS else ""))
    return f"{type(exc).__name__}: {exc}"


def _connect(cfg: dict):
    """Connect (+ STARTTLS + login) and hand back the ready session."""
    host, port, security = cfg["host"], cfg["port"], cfg["security"]
    server = (smtplib.SMTP_SSL(host, port, timeout=25) if security == "ssl"
              else smtplib.SMTP(host, port, timeout=25))
    if security == "starttls":
        server.starttls()
    if cfg["username"]:
        server.login(cfg["username"], cfg["password"])
    return server


def _deliver(msg: EmailMessage, cfg: dict, lang: str | None = None) -> tuple[bool, str]:
    """Connect, authenticate, send. Returns (ok, human-readable detail)."""
    try:
        with _connect(cfg) as smtp:
            smtp.send_message(msg)
        return True, pick(f"{cfg['host']}:{cfg['port']} 已接受这封邮件 / accepted this message", lang)
    except Exception as exc:  # noqa: BLE001 — network, TLS, DNS, timeout, refusal …
        return False, _explain(exc, lang)


def _deliver_all(messages: list[tuple[str, EmailMessage]],
                 cfg: dict, lang: str | None = None) -> list[dict]:
    """
    Several messages over ONE connection, with a per-message result.

    Why one connection: the admin's "send test" fires three templates back to back, and
    three logins in a few seconds is exactly what a provider's throttle counts — the mail
    server closes the socket or answers 421 and the *third* message is the one that gets
    lost. That is the shape of the report "I only got two of the three". One login, three
    sends, one quit.
    """
    host, port = cfg["host"], cfg["port"]
    try:
        smtp = _connect(cfg)
    except Exception as exc:  # noqa: BLE001 — could not even get a session
        detail = _explain(exc, lang)
        return [{"kind": kind, "status": "failed", "detail": detail} for kind, _ in messages]
    out: list[dict] = []
    try:
        for kind, msg in messages:
            try:
                smtp.send_message(msg)
                out.append({"kind": kind, "status": "sent",
                            "detail": pick(f"{host}:{port} 已接受这封邮件 / accepted this message", lang)})
            except Exception as exc:  # noqa: BLE001 — one bad message must not stop the rest
                out.append({"kind": kind, "status": "failed", "detail": _explain(exc, lang)})
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001 — closing a broken session must not raise
            pass
    return out


def send_agent_code(to_email: str, code: str, label: str = "",
                    skill_url: str = "", asset_base: str = "") -> str:
    """
    Email an agent access code (授权码). Same contract as the verification code:
    'sent' / 'unconfigured' / 'failed', never a pretend success.
    """
    cfg = mail_config.current()
    if not mail_config.is_configured(cfg):
        print(f"WARNING: SMTP is not configured; the agent access code for {to_email} "
              f"was not emailed (source={cfg['source']})", flush=True)
        return "unconfigured"
    msg = _build_message_from(
        mail_templates.agent_code_email(code, label, to_email, skill_url,
                                        _banner_url("email-banner-agent-code.jpg",
                                                    "email-banner-agent-code.png",
                                                    base=asset_base)),
        to_email, cfg["sender"])
    ok, detail = _deliver(msg, cfg)
    if ok:
        return "sent"
    _LOG.warning("could not email the agent access code to %s: %s", to_email, detail)
    return "failed"


def send_verification_code(to_email: str, code: str, asset_base: str = "") -> str:
    """
    Try to deliver the code. Returns:

      'sent'         the SMTP server accepted it
      'unconfigured' nothing to send with — the caller must surface the code
                     another way (the admin page lists live codes, and the router
                     prints it to stdout because `core/logring` swallows app logs)
      'failed'       the server refused; the code is still valid
    """
    cfg = mail_config.current()
    if not mail_config.is_configured(cfg):
        # print(), not _LOG: a root handler in core/logring.py suppresses Python's
        # last-resort handler, so app-level logging never reaches stdout.
        print(f"WARNING: SMTP is not configured; the registration code for {to_email} "
              f"was not emailed (source={cfg['source']}, "
              f"password_readable={cfg['password_readable']})", flush=True)
        return "unconfigured"

    ok, detail = _deliver(_build_message(to_email, code, cfg["sender"], asset_base), cfg)
    if ok:
        _LOG.info("verification code emailed to %s", to_email)
        return "sent"
    _LOG.warning("could not email the verification code to %s: %s", to_email, detail)
    return "failed"


def send_invite(to_email: str, code: str, ttl_days: int, app_url: str,
                invited_by: str = "", asset_base: str = "") -> str:
    """
    Email an invitation to a colleague who has no account yet. Same contract as the
    other two: 'sent' / 'unconfigured' / 'failed' — never a pretend success, because
    the caller has to fall back to showing the code once when mail is not working.
    """
    cfg = mail_config.current()
    if not mail_config.is_configured(cfg):
        print(f"WARNING: SMTP is not configured; the invitation for {to_email} was not "
              f"emailed (source={cfg['source']})", flush=True)
        return "unconfigured"
    msg = _build_message_from(
        mail_templates.invite_email(code, ttl_days, to_email, app_url, invited_by,
                                    _banner_url("email-banner-invite.jpg",
                                                "email-banner-invite.png",
                                                base=asset_base)),
        to_email, cfg["sender"])
    ok, detail = _deliver(msg, cfg)
    if ok:
        return "sent"
    _LOG.warning("could not email the invitation to %s: %s", to_email, detail)
    return "failed"


def send_test(to_email: str, skill_url: str = "", asset_base: str = "",
              invited_by: str = "", lang: str | None = None) -> tuple[str, str, list[dict]]:
    """
    Admin-initiated preview. Returns (status, detail, per-template results).

    It sends the **real templates** with sample codes rather than a generic "SMTP
    works" note: the operator's actual question is "what will the recipient see",
    and the only place that answers it is a real inbox (mail clients mangle layout
    in ways a browser never shows). `detail` still carries the SMTP server's own
    words — when a mailbox refuses us the admin needs "526 Authentication failure"
    plus what it means, not just 'failed'.
    """
    cfg = mail_config.current()
    if not mail_config.is_configured(cfg):
        missing = []
        if not cfg["host"]:
            missing.append("SMTP 服务器地址未填")
        if not cfg["sender"]:
            missing.append("发信地址未填")
        if not cfg["password_readable"]:
            missing.append("已存的密码无法解密（SECRET_KEY 是否轮换过？请重新填一次密码）")
        return "unconfigured", "；".join(missing) or "配置不完整", []

    minutes = max(1, settings.AUTH_CODE_TTL_SECONDS // 60)
    samples = [
        ("verification code",
         mail_templates.verification_code_email(
             "314159", minutes, to_email,
             _banner_url("email-banner-verification.jpg", "email-banner-verification.png",
                         base=asset_base),
             app_url=_app_url(asset_base))),
        ("agent access code",
         mail_templates.agent_code_email("klado_agent_SAMPLE0NOT0AREAL0CODE000000",
                                         "laptop", to_email, skill_url,
                                         _banner_url("email-banner-agent-code.jpg",
                                                     "email-banner-agent-code.png",
                                                     base=asset_base))),
        # The invitation too: it is the one an admin sends to somebody else, so
        # "what will they see" matters even more here than for the others.
        ("invitation",
         mail_templates.invite_email(
             "831742", max(1, settings.AUTH_INVITE_TTL_SECONDS // 86400), to_email,
             _app_url(asset_base),
             invited_by=invited_by or "Klado",
             banner_url=_banner_url("email-banner-invite.jpg",
                                    "email-banner-invite.png", base=asset_base))),
    ]
    results = _deliver_all(
        [(kind, _build_message_from(template, to_email, cfg["sender"]))
         for kind, template in samples], cfg, lang)
    failed = [r for r in results if r["status"] != "sent"]
    if not failed:
        return "sent", f"已发送 {len(results)} 封示例邮件到 {to_email}", results
    return "failed", failed[0]["detail"], results