"""
HTML email bodies.

Why HTML, and why English: these arrive in a corporate mailbox, often on a phone,
and the recipient is usually deciding "is this real?" in two seconds. A plain-text
two-liner reads like a spam attempt, so both the verification code and the agent
access code get a small, self-contained card: who it is from, the code in a box you
can copy, exactly what to do with it, and what to do if it was not you.

Layout rules (email clients are not browsers):
* one 600px table, everything centred with `align`/`width`, no flex/grid, no external
  CSS, no webfonts, no images — several clients strip or block those;
* inline styles only, and nothing load-bearing rests on `border-radius`;
* every message also ships a plain-text alternative (multipart/alternative), because
  some internal gateways rewrite or drop HTML.
"""
from __future__ import annotations

import textwrap

BRAND = "Klado"
BLUE = "#2563eb"
INK = "#0f172a"
MUTED = "#64748b"
BORDER = "#e2e8f0"
PANEL = "#f1f5f9"

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',"
         "Arial,sans-serif")
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"


def _banner(banner_url: str) -> str:
    """
    Optional full-width image at the top of the card.

    Emitted only when a URL is supplied — the caller checks that the file exists, so an
    email never ships a broken image just because a banner has not been generated yet.
    `alt` carries the wordmark, so a client that blocks images shows something sensible.
    """
    if not banner_url:
        return ""
    return ('      <tr><td style="padding:0;font-size:0;line-height:0;">'
            f'<img src="{banner_url}" alt="{BRAND}" width="600" height="150" '
            'style="display:block;width:100%;max-width:600px;height:auto;border:0;'
            'outline:none;text-decoration:none;border-radius:10px 10px 0 0;">'
            '</td></tr>\n')


def _layout(title: str, preheader: str, blocks: str, banner_url: str = "") -> str:
    """Wrap content in the shared card. `preheader` is the grey line inboxes show."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;">
<!-- preheader: shown in the inbox list, hidden in the body -->
<div style="display:none;font-size:1px;color:#f8fafc;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f8fafc;">
  <tr>
    <td align="center" style="padding:28px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#ffffff;border:1px solid {BORDER};border-radius:10px;">
{_banner(banner_url)}        <tr>
          <td style="padding:22px 28px 0;font-family:{_FONT};">
            <div style="font-size:15px;font-weight:700;letter-spacing:.06em;color:{INK};">{BRAND}</div>
            <div style="height:3px;width:44px;background:{BLUE};margin-top:10px;border-radius:2px;"></div>
          </td>
        </tr>
        <tr>
          <td style="padding:20px 28px 6px;font-family:{_FONT};font-size:14px;line-height:1.65;color:{INK};">
            {blocks}
          </td>
        </tr>
        <tr>
          <td style="padding:18px 28px 24px;font-family:{_FONT};font-size:12px;line-height:1.6;color:{MUTED};border-top:1px solid {BORDER};">
            This is an automated message from {BRAND}. Replies to this address are not read.
          </td>
        </tr>
      </table>
      <div style="font-family:{_FONT};font-size:11px;color:{MUTED};padding-top:14px;">
        {BRAND} &middot; your analytics team
      </div>
    </td>
  </tr>
</table>
</body>
</html>"""


def _code_box(code: str) -> str:
    return (f'<div style="margin:18px 0 6px;padding:16px 18px;background:{PANEL};'
            f'border:1px solid {BORDER};border-radius:8px;text-align:center;">'
            f'<div style="font-family:{_MONO};font-size:24px;font-weight:700;'
            f'letter-spacing:4px;color:{INK};word-break:break-all;">{code}</div></div>')


def _option_label(name: str, what: str) -> str:
    """`Option 1 — <what>`. Numbered lists alone were ambiguous: the
    message had an outer 1./2. and an inner 1./2./3. that read as the same level."""
    return (f'<div style="margin-top:22px;padding-top:14px;border-top:1px solid {BORDER};">'
            f'<div style="font-family:{_FONT};font-size:13px;font-weight:700;color:{BLUE};'
            f'letter-spacing:.04em;text-transform:uppercase;">{name}</div>'
            f'<div style="font-family:{_FONT};font-size:14px;font-weight:600;color:{INK};'
            f'padding-top:2px;">{what}</div></div>')


def _steps(items: list[str]) -> str:
    """`Step 1 — …`. Always spelled out, never a bare number: see _option_label()."""
    rows = "".join(
        f'<tr><td style="padding:0 0 8px;font-family:{_FONT};font-size:14px;'
        f'line-height:1.6;color:{INK};"><b>Step {i} &mdash;</b> {t}</td></tr>'
        for i, t in enumerate(items, 1))
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin-top:12px;">{rows}</table>')


def _cautions(items: list[str]) -> str:
    rows = "".join(
        f'<tr><td style="padding:0 0 8px;font-family:{_FONT};font-size:13px;'
        f'line-height:1.6;color:{MUTED};">&bull;&nbsp; {t}</td></tr>'
        for t in items)
    return (f'<div style="margin-top:20px;padding-top:16px;border-top:1px solid {BORDER};">'
            f'<div style="font-family:{_FONT};font-size:11px;font-weight:700;'
            f'letter-spacing:.08em;color:{MUTED};text-transform:uppercase;'
            f'padding-bottom:8px;">Good to know</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">{rows}</table></div>')


def verification_code_email(code: str, ttl_minutes: int, to_email: str,
                            banner_url: str = "", app_url: str = "") -> tuple[str, str, str]:
    """
    (subject, html, text) for the sign-up verification code.

    `app_url` puts the app's own address in the mail as a clickable link. It matters more
    here than anywhere else: the code lives ten minutes, so by the time the mail is read
    the sign-up page is often closed — "go back to the sign-up page" is then an
    instruction with no destination.
    """
    subject = f"Your {BRAND} verification code"
    preheader = f"Your code is {code} — it expires in {ttl_minutes} minutes."
    link = (f'<a href="{app_url}" style="color:{BLUE};word-break:break-all;">{app_url}</a>'
            if app_url else "")
    blocks = (
        f'<div style="font-size:17px;font-weight:700;padding-bottom:6px;">Welcome to {BRAND}</div>'
        f'<div style="padding-bottom:2px;">Use this code to finish creating the account for '
        f'<b>{to_email}</b>:</div>'
        + _code_box(code)
        + f'<div style="text-align:center;font-family:{_FONT};font-size:12px;color:{MUTED};">'
          f'Valid for {ttl_minutes} minutes, single use.</div>'
        + f'<div style="margin-top:22px;font-weight:600;">How to use it</div>'
        + (_steps([
            f'Open {link}',
            f'Enter <b>{to_email}</b> and paste the code above.',
            'Choose a password — at least 8 characters — and you are in.',
        ]) if app_url else _steps([
            "Go back to the sign-up page (it should still be open in your browser).",
            "Paste the code into the verification field.",
            "Choose a password — at least 8 characters — and you are in.",
        ]))
        + _cautions([
            "The code can be used once, and it expires in "
            f"{ttl_minutes} minutes.",
            "Only approved work addresses are accepted at the moment.",
            "If you did not ask for this, you can ignore this email — "
            "nobody can create an account with your address without this code.",
            "The same code is also visible to an administrator in the app, in case "
            "the email is filtered.",
        ])
    )
    text = (f"{BRAND} — verification code\n\n"
            f"Welcome to {BRAND}.\n\n"
            f"Code: {code}\n"
            f"Valid for {ttl_minutes} minutes, single use.\n\n"
            f"How to use it:\n"
            + (f"  1. Open {app_url}\n"
               f"  2. Enter {to_email} and paste the code above.\n"
               if app_url else
               f"  1. Go back to the sign-up page.\n"
               f"  2. Paste the code into the verification field.\n")
            + f"  3. Choose a password (at least 8 characters).\n\n"
            f"If you did not ask for this, ignore this email.\n")
    return subject, _layout(subject, preheader, blocks, banner_url), text


def _prompt_lines(code: str, skill_url: str) -> list[str]:
    """
    The prompt the human pastes — the single source of truth for the HTML panel and the
    plain-text body, so the two can never drift apart.

    There is deliberately no "install the skill first" step: pasting this block is the
    only action, and the package address travels *inside* the prompt as a reference the
    assistant can look up. Nothing here tells the recipient to set anything up.

    The third line exists because assistants keep credentials in session memory by
    default and ask for them again in every new chat (observed with Doubao Work,
    2026-09-26 — the observation is why the line is there; the wording is not tied to
    that product). Telling it to persist the code *in this block* is what makes the paste
    a one-time event; the same convention is documented in the skill's Authentication
    section, so the two reinforce each other.
    """
    ref = (f"Use the Klado skill (reference: {skill_url})." if skill_url
           else "Use the Klado skill.")
    return [
        ref,
        f"Use this access code to reach Klado: {code}",
        "Save it now to ~/.config/klado/agent_code (permissions 600) and read it "
        "from there in every future session — do not ask me for it again.",
        "Read the knowledge base and the business data with it, answer my questions, "
        "and publish the analysis as a report card.",
        "Note: the code is read-only (it can only publish or update reports) — keep it "
        "out of the skill package and never write it into a report you publish.",
    ]


def _prompt_block(code: str, skill_url: str) -> str:
    """The one thing the human has to paste, rendered as a panel you can copy as-is."""
    text = "\n".join(_prompt_lines(code, skill_url))
    return (f'<div style="margin-top:6px;padding:12px 14px;background:{PANEL};'
            f'border:1px solid {BORDER};border-left:3px solid {BLUE};border-radius:6px;'
            f'font-family:{_FONT};font-size:13px;line-height:1.7;color:{INK};'
            f'white-space:pre-wrap;word-break:break-word;">{text}</div>')


def agent_code_email(code: str, label: str, to_email: str,
                     skill_url: str = "", banner_url: str = "") -> tuple[str, str, str]:
    """
    (subject, html, text) for an agent access code (授权码).

    Deliberately minimal: one sentence about the assistant plus the prompt to paste — no
    install step, nothing to set up. The code is **not** printed on its own line: it
    travels inside the prompt, which is the only thing the recipient has to copy, so
    there is nothing to copy wrongly.

    Two things are named by *role*, never by product, and that is a deliberate
    correction rather than a style choice:

    * "your AI assistant", not "Doubao Work / 豆包工作". The same code works in any
      assistant that can run a pasted prompt, and a mail that names one vendor both
      misleads everyone else and dates itself the moment they switch. The recipient is
      the one who knows which assistant they use.
    * `label` is the **token's own name inside the account** (`signup`, or whatever the
      admin typed when minting a second code) — a bookkeeping label the recipient has
      never seen and cannot act on. Printing it made the live signup mail read "an
      access code was issued for signup", which explains nothing. It is now confined to
      the small print where it can only help ("this is the code called X"), and the
      headline just says who it is for.
    """
    subject = f"Your {BRAND} agent access code"
    preheader = (f"Paste the prompt below into your AI assistant to let it work "
                 f"with {BRAND}.")
    who = ""
    blocks = (
        f'<div style="font-size:17px;font-weight:700;padding-bottom:6px;">'
        f'An agent access code was issued{who}</div>'
        f'<div style="padding-bottom:2px;">It lets that assistant work with {BRAND} on '
        f'behalf of <b>{to_email}</b>, without ever knowing the account password.</div>'
        + f'<div style="margin-top:22px;font-weight:600;">How to use it</div>'
        + f'<div style="padding-top:8px;">All you need is to paste the prompt below into '
          f'<b>your AI assistant</b> — whichever one you use.</div>'
        + _prompt_block(code, skill_url)
        + _cautions([
            "Use it only on <b>approved devices and networks</b>. Do not share the "
            "code, and do not copy business data outside the approved environment — "
            "this access exists so that the data stays inside your organisation.",
            "It can <b>not</b> sign in to the web interface — it only works for the API, "
            "so it cannot be turned into a browser session.",
            "Never put the code inside a report you publish: published documents are "
            "readable without signing in.",
            ("This code is listed in the app under <b>Agent access</b>"
             + (f' as &ldquo;<b>{label}</b>&rdquo;' if label else "")
             + ". It has no expiry — ask the sender to revoke it there when you are "
               "finished with it."),
        ])
    )
    prompt = "\n".join(
        textwrap.fill(line, width=74, initial_indent="   ", subsequent_indent="     ",
                      break_long_words=False, break_on_hyphens=False)
        for line in _prompt_lines(code, skill_url))
    text = (f"{BRAND} — agent access code\n\n"
            f"An agent access code was issued for the account {to_email}.\n"
            f"It lets that assistant work with {BRAND} without knowing the password.\n\n"
            f"How to use it\n"
            f"All you need is to paste the prompt below into your AI assistant —\n"
            f"whichever one you use:\n"
            f"   ------------------------------------------------------------\n"
            f"{prompt}\n"
            f"   ------------------------------------------------------------\n\n"
            f"Good to know\n"
            f"  - Use it only on approved devices and networks. Do not share the\n"
            f"    code, and do not copy business data outside the approved environment.\n"
            f"  - It cannot sign in to the web interface — it only works for the API.\n"
            f"  - Never put the code inside a report you publish.\n"
            + (f"  - Listed in the app as '{label}'. " if label else "  - ")
            + f"No expiry; ask the sender to revoke it when you are done.\n")
    return subject, _layout(subject, preheader, blocks, banner_url), text


def _feature_list(items: list[str]) -> str:
    """A plain bullet list for "what you can do here" — deliberately not `_cautions`:
    muted grey is right for warnings and wrong for the pitch."""
    rows = "".join(
        f'<tr><td style="padding:0 0 9px;font-family:{_FONT};font-size:14px;'
        f'line-height:1.65;color:{INK};"><span style="color:{BLUE};font-weight:700;'
        f'">&rsaquo;</span>&nbsp; {t}</td></tr>'
        for t in items)
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin-top:10px;">{rows}</table>')


# The pitch, in one place: the HTML card and the plain-text body both render this
# list, so the two cannot drift (the lesson from the agent-code prompt block).
_FEATURES = [
    "Look up any SKU — specifications, features, CMF, value class, list price — "
    "without digging through files.",
    "Compare against competitors feature by feature, not just on price.",
    "Follow the market and our own numbers — sales, inventory, and the reporting "
    "behind them — in one place.",
    "Track NPD and the monthly review in the same place.",
    "Ask the built-in assistant in plain language and turn the answer into a "
    "report you can share as a link.",
]


def invite_email(code: str, ttl_days: int, to_email: str, app_url: str,
                 invited_by: str = "", banner_url: str = "") -> tuple[str, str, str]:
    """
    (subject, html, text) for an invitation to a colleague who has no account yet.

    Three things it has to do, in this order: welcome them, say what the thing is
    for, and make registering one straight step. The code travels in the mail
    (long-lived on purpose — see AUTH_INVITE_TTL_SECONDS), so there is no second
    round trip where the invitee has to ask for a code of their own.
    """
    subject = f"You're invited to {BRAND}"
    preheader = f"{invited_by or 'A colleague'} invited you to {BRAND} — here is your code."
    by = f"<b>{invited_by}</b> invited you" if invited_by else "You have been invited"
    blocks = (
        f'<div style="font-size:17px;font-weight:700;padding-bottom:6px;">'
        f'Welcome to {BRAND}</div>'
        f'<div style="padding-bottom:2px;">{by} — the product &amp; market hub for '
        f'our business. One place for the product data, the '
        f'competitor picture and the numbers behind our decisions, with an assistant '
        f'that answers questions in plain language.</div>'
        + f'<div style="margin-top:22px;font-weight:600;">What you can do here</div>'
        + _feature_list(_FEATURES)
        + f'<div style="margin-top:22px;font-weight:600;">Your invitation code</div>'
        + f'<div style="padding-bottom:2px;">Use it to create the account for '
          f'<b>{to_email}</b>:</div>'
        + _code_box(code)
        + f'<div style="text-align:center;font-family:{_FONT};font-size:12px;color:{MUTED};">'
          f'Valid for {ttl_days} days, single use.</div>'
        + f'<div style="margin-top:22px;font-weight:600;">How to join</div>'
        + _steps([
            f'Open <a href="{app_url}" style="color:{BLUE};word-break:break-all;">'
            f'{app_url}</a>',
            f'Enter <b>{to_email}</b> and paste the code above.',
            'Choose a password — at least 8 characters — and you are in.',
        ])
        + _cautions([
            f'The code only works for <b>{to_email}</b>. Register with your '
            f'work address, not a private one.',
            f'It is single use and expires in {ttl_days} days — ask '
            f'{invited_by or "the sender"} for a new one if it lapses.',
            "Please keep business data on approved devices and networks.",
            "If this reached you by mistake, just ignore it: nobody can create an "
            "account without this code.",
        ])
    )
    text = (f"{BRAND} — you're invited\n\n"
            f"{(invited_by + ' invited you') if invited_by else 'You have been invited'} "
            f"to {BRAND}, the product & market hub for our business.\n"
            f"One place for the product data, the competitor picture and the numbers "
            f"behind our decisions.\n\n"
            f"What you can do here\n"
            + "".join(textwrap.fill(f"- {item}", width=74, subsequent_indent="    ",
                                    break_long_words=False, break_on_hyphens=False) + "\n"
                      for item in _FEATURES)
            + f"\nYour invitation code\n"
            f"{code}\n"
            f"Valid for {ttl_days} days, single use. It only works for {to_email}.\n\n"
            f"How to join\n"
            f"  1. Open {app_url}\n"
            f"  2. Enter {to_email} and paste the code above.\n"
            f"  3. Choose a password (at least 8 characters).\n\n"
            f"If this reached you by mistake, ignore it — nobody can create an account\n"
            f"without this code.\n")
    return subject, _layout(subject, preheader, blocks, banner_url), text
