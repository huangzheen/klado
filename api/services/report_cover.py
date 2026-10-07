"""
Report covers — build the editorial prompt, call the image model, return a
card-ready JPEG.

Why this lives on the server
---------------------------
The agent that writes reports is **external**: it reads the knowledge base over
HTTP and publishes over HTTP, and it holds no image credentials. So the project
exposes generation here and the agent only has to send a title.

⚠️ Keep the prompt in this file and in the knowledge document
(`klado-v2:media-image-prompt`) in sync — the doc is what the external agent
reads, this is what actually runs. Changing one without the other is how the two
silently drift.

Two prompt variants
-------------------
* **Default (`with_title=False`)** — the image carries NO text. The Workspace card
  renders the title itself (cover image on top, title below), so baking the title
  in would show it twice. This is the variant to use for a card cover.
* **`with_title=True`** — the magazine-cover variant with the title drawn into the
  image, for standalone/hero use where nothing else renders a title.

Generation goes through MiniMax with the deployment's coding-plan key. Contract taken
from the official doc (https://platform.minimax.cn/docs/api-reference/image-generation-t2i)
and confirmed by calling it:

* `POST {base}/v1/image_generation`, `Authorization: Bearer <key>`
* `model` is only `image-01` or `image-01-live`; `MiniMax-M3` / `image-02` come back
  2013 "unsupported model" (M3 is a chat model)
* `aspect_ratio` → 16:9 is exactly 1280x720; the full enum is in ASPECTS below
* `prompt` is capped at **1500 characters** — enforced here before the call
* `response_format: base64` returns the bytes inline; the `url` form expires in 24h
* errors arrive as HTTP 200 with `base_resp.status_code != 0` (see _ERROR_HINTS) The default aspect is 16:9 — the same canvas shape as the card's cover box and as
a report deck, so one ratio rules both.
"""
from __future__ import annotations

import base64
import io
import logging
import os

from services import llm_config

_LOG = logging.getLogger(__name__)

# MiniMax takes one `aspect_ratio` string (not a pixel size). These are the values
# the endpoint accepts; anything else falls back to 16:9.
ASPECTS: tuple[str, ...] = ("1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9")
DEFAULT_RATIO = "16:9"     # the card's cover box and a report deck share this shape
MAX_WIDTH = 1600           # downscale before storing: keeps rows and payloads sane
JPEG_QUALITY = 84

# ── The prompt ────────────────────────────────────────────────────────────────
#
# The cover is the first thing a reader sees in Workspace, so its colour sets the
# tone before a single word of the title is read. That palette is fixed here — one
# neutral editorial style shared by every cover — so nothing in the prompt depends
# on a particular company or business line.
#
# ⚠️ Keep this in sync with the knowledge document `klado-v2:media-image-prompt`
# §报告封面 — the doc is what the external agent reads, this file is what runs.

# The one neutral palette every cover is built from.
_PALETTE: dict[str, str] = {
    "background": "冷白、米白、略带细微纸张纹理",
    "palette": "冷白、石墨黑、炭灰，主色是低饱和的深石墨蓝（近似 #2B3A55，沉稳不刺眼的蓝灰，不要偏紫、不要偏亮或偏暗），全图只用这一种主色",
    "material": "哑光金属、精密黑色模块、磨砂半透明玻璃",
    "main_color": "低饱和的深石墨蓝（#2B3A55）",
    "title_accent": "低饱和深石墨蓝",
}


_VISUAL_LANGUAGE = """【固定视觉语言】
- 整体气质：高级商业编辑视觉、现代瑞士平面设计、克制的科技杂志感。
- 背景：{background}。
- 配色仅限：{palette}。
- **主色的用法（重要）**：画面中面积最大的那块材质（装置主体）必须用主色 {main_color}；
  石墨黑与炭灰只能用于结构件、小部件、阴影和背景细节 —— **不要让深灰占据主体面积**，
  否则主体会失去应有的颜色重点。
- 材质：{material}、柔和自然阴影。
- 构图必须安静、精确、有大量留白，像一件被摆进画廊的编辑雕塑。
- 禁止赛博朋克、霓虹、蓝紫渐变、机器人、AI 大脑、芯片、科幻城市、复杂仪表盘、满屏 UI、卡通、家居风、廉价 3D、夸张光效。
- 除上面列出的色相外不要引入任何其它颜色，保持单一克制的配色。"""

_CREATIVE_TASK = """【核心创作任务】
先理解标题背后真正要表达的关系、变化、动作或结果。

然后把它转译成一个"可读但不直白"的抽象实体意象：
- 必须由一个清晰的核心动作构成，例如连接、汇聚、对齐、推进、拆解、穿透、折叠、平衡、转化、生长或跨越。
- 画面中的物体要有明确的隐喻关系，让观众即使不看标题，也能感到"某件事正在发生"。
- 只讲一个视觉故事，使用少量精确的物体，不堆砌概念。
- 不要把它画成图标、插画说明书或字面场景；要把它做成一个有材质、有结构、有动作的抽象小型装置。
- 可加入一位小比例、真实感的人物，负责与装置发生关键动作（调整、连接、推进、观察或完成最后一步），只作为尺度和行动感，不抢主题；若会显得俗套则省略。"""

_NO_TEXT = """【排版】
- 画面中**不得出现任何文字**：没有标题、没有字母、没有数字、没有副标题、没有 Logo、没有水印、没有边框或装饰性小字。
- 主体居中偏右，左侧保留大面积留白，整体保持单一视觉中心。"""

_WITH_TITLE = """【排版】
- 标题必须逐字准确使用：{title}
- 标题使用高级中文无衬线字体，深石墨色；可自动挑选标题中最关键的一个词或数字，使用{title_accent}强调。
- 横向比例：标题在左，抽象意象在右；标题约占 50%–55%，意象约占 40%–45%。
- 竖向比例：标题在上，抽象意象在下；仍然保持大面积留白和单一视觉中心。
- 根据标题长度自动断行，确保中文清晰、无错别字。
- 除标题外，不要生成任何可读文字、Logo、水印、副标题、边框或装饰性小字。"""

_HEADER = """你是一位高级商业科技媒体的视觉总监。请根据下面的主题，生成一张高级、极简、可读的编辑视觉封面。

【输入】
主题 / 标题：{title}
画幅比例：{ratio}"""


def build_prompt(title: str, ratio: str = DEFAULT_RATIO, with_title: bool = False,
                 extra: str = "") -> str:
    """Assemble the visual-director brief for one cover."""
    title = (title or "").strip()
    ratio = ratio if ratio in ASPECTS else DEFAULT_RATIO
    palette = _PALETTE
    parts = [
        _HEADER.format(title=title, ratio=ratio),
        _CREATIVE_TASK,
        _VISUAL_LANGUAGE.format(background=palette["background"], palette=palette["palette"],
                                material=palette["material"], main_color=palette["main_color"]),
        (_WITH_TITLE.format(title=title, title_accent=palette["title_accent"])
         if with_title else _NO_TEXT),
    ]
    if extra.strip():
        parts.append("【补充要求】\n" + extra.strip())
    # ratio is stated once in the header; repeating it verbatim at the end is the
    # one other place it belongs (see the knowledge doc's rule about not scattering it).
    parts.append(f"画幅比例：{ratio}（与开头一致，不要改变比例）。")
    return "\n\n".join(parts)


# ── Generation ───────────────────────────────────────────────────────────────
#
# The generator is MiniMax (`POST {MINIMAX_BASE_URL}/image_generation`), not
# Volcengine: the Seedream path in routers/research.py belonged to a module that is
# no longer used, and the key provisioned for this deployment is the MiniMax coding
# plan — verified to include image generation on the code-plan key.
#
# ⚠️ MiniMax answers **HTTP 200 even when it fails**: the real verdict is
# `base_resp.status_code` (0 = ok; e.g. 2013 = unsupported model) with `data: null`.
# Trusting the HTTP status alone turns "wrong model name" into a success followed by
# an unpacking crash. Trust the body, never the status line.
#
# `response_format: "base64"` avoids a second round trip: the endpoint hands back
# the JPEG inline instead of an OSS URL we would have to download.

MINIMAX_PATH = "/image_generation"
MAX_PROMPT_CHARS = 1500          # documented cap; exceeding it returns 2013 "invalid params"

# Documented base_resp.status_code values → what the publisher should actually do.
_ERROR_HINTS = {
    1002: "触发限流，请稍后重试",
    1004: "账号鉴权失败，检查 API-Key",
    1008: "账号余额不足",
    1026: "图片描述涉及敏感内容：请改写标题或补充要求后重试",
    2013: "参数异常（检查 model / aspect_ratio / prompt 长度）",
    2049: "无效的 API key",
}


def _minimax_key() -> str:
    return os.environ.get("MINIMAX_API_KEY", "")


def _image_model() -> str:
    """The MiniMax image model to call (`MINIMAX_IMAGE_MODEL`, default `image-01`)."""
    return llm_config.get()["image_generation_model"]


def _enforce_prompt_limit(prompt: str, extra: str) -> str:
    """
    Keep the prompt inside MiniMax's 1500-character cap.

    The agent-supplied `extra` is dropped first (it is the only part we can discard
    without changing whose story the cover tells). If the brief is *still* too long,
    raise a ValueError that the API surfaces as a 400 — better than letting MiniMax
    answer 2013 "invalid params", which reads like a server bug.
    """
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    if extra.strip():
        trimmed = build_prompt(_TITLE_HOLDER[0], _TITLE_HOLDER[1], _TITLE_HOLDER[2])
        if len(trimmed) <= MAX_PROMPT_CHARS:
            _LOG.warning("cover prompt over %d chars; dropped the caller's extra requirement",
                         MAX_PROMPT_CHARS)
            return trimmed
        prompt = trimmed
    raise ValueError(
        f"封面提示词 {len(prompt)} 字符，超过 MiniMax 的 {MAX_PROMPT_CHARS} 上限 —— 请缩短标题（或附加要求）"
        f" / cover prompt is {len(prompt)} characters, over MiniMax's {MAX_PROMPT_CHARS} limit — "
        "shorten the title (or the extra requirement)")


def _call_minimax(prompt: str, ratio: str, seed: int | None = None) -> bytes:
    """One generation call; returns raw image bytes. Raises RuntimeError on failure."""
    import requests

    key = _minimax_key()
    if not key:
        raise RuntimeError("未配置 MINIMAX_API_KEY，无法生成封面 / MINIMAX_API_KEY is not configured; cannot generate a cover")
    base = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1").rstrip("/")
    model = _image_model()
    payload = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": ratio,
        "n": 1,
        "response_format": "base64",
        # The brief is curated by hand: do not let the model rewrite it, and do not
        # burn a watermark onto a cover that will sit on a card wall.
        "prompt_optimizer": False,
        "aigc_watermark": False,
    }
    if seed:
        payload["seed"] = int(seed)      # same seed + params ≈ the same cover again
    try:
        resp = requests.post(
            base + MINIMAX_PATH,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001 — network layer
        raise RuntimeError(f"MiniMax request failed: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"MiniMax HTTP {resp.status_code}: {resp.text[:180]}")

    body = resp.json()
    base_resp = body.get("base_resp") or {}
    code = base_resp.get("status_code")
    if code not in (0, None):
        hint = _ERROR_HINTS.get(code)
        detail = base_resp.get("status_msg") or ""
        raise RuntimeError(
            f"MiniMax error {code}: {detail}" + (f" — {hint}" if hint else "")
            + f" (model={model})")
    images = (body.get("data") or {}).get("image_base64") or []
    if not images:
        raise RuntimeError(f"MiniMax returned no image (model={model})")
    return base64.b64decode(images[0])


def _to_jpeg(raw: bytes) -> tuple[bytes, int, int]:
    """Normalise whatever the endpoint returns into a card-ready JPEG."""
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), img.width, img.height


# `_enforce_prompt_limit` has to rebuild the prompt without `extra`; rather than
# thread three more arguments through, the current call's title/ratio/variant are
# parked here for that one rebuild.
_TITLE_HOLDER: tuple[str, str, bool] = ("", DEFAULT_RATIO, False)


def generate_cover(title: str, ratio: str = DEFAULT_RATIO, with_title: bool = False,
                   extra: str = "", seed: int | None = None) -> dict:
    """
    Generate one cover. Returns

        {"prompt", "image_base64", "mime", "width", "height", "model", "aspect_ratio"}

    Raises RuntimeError with a readable message when the key is missing or MiniMax
    rejects the request — the caller turns that into a 502.
    """
    global _TITLE_HOLDER
    ratio = ratio if ratio in ASPECTS else DEFAULT_RATIO
    _TITLE_HOLDER = ((title or "").strip(), ratio, with_title)
    prompt = build_prompt(title, ratio, with_title, extra)
    prompt = _enforce_prompt_limit(prompt, extra)
    raw = _call_minimax(prompt, ratio, seed)
    jpeg, width, height = _to_jpeg(raw)
    return {
        "prompt": prompt,
        "image_base64": base64.b64encode(jpeg).decode("ascii"),
        "mime": "image/jpeg",
        "width": width,
        "height": height,
        "model": _image_model(),
        "aspect_ratio": ratio,
    }


def decode_upload(cover_base64: str) -> tuple[bytes, int, int]:
    """Normalise an agent-supplied image (data URI or bare base64) into a JPEG."""
    payload = (cover_base64 or "").strip()
    if "," in payload[:80] and payload.lstrip().startswith("data:"):
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cover_base64 不是合法的 base64: {exc} / cover_base64 is not valid base64: {exc}") from exc
    if not raw:
        raise ValueError("cover_base64 解码后是空图片 / cover_base64 decoded to an empty image")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("封面图片超过 8 MB / cover image is larger than 8 MB")
    return _to_jpeg(raw)


# ── One resolver, two features ────────────────────────────────────────────────
#
# Reports and calendar events store a cover with the SAME columns and the same request
# fields, so they must resolve it with the same code — two copies would drift the day one
# of them learns something (the report side already carries the `COVER_ALIASES` story of a
# cover that was silently dropped). `routers/reports.py` and `routers/calendar.py` both
# call this and only differ in how they map the errors.

def resolve_cover(body, title: str) -> dict | None:
    """Resolve the cover for a publish/update request body.

    Priority: explicit bytes → explicit URL → generate (only when asked). Returns None when
    nothing was supplied, so a re-write keeps the existing cover instead of dropping it.

    Raises ValueError for bad input (undecodable image) — the caller turns that into a
    400 — and RuntimeError when the provider refuses (→ 502).
    """
    supplied = getattr(body, "cover_base64", None)
    if supplied:
        data, width, height = decode_upload(supplied)
        return {"data": data, "mime": "image/jpeg", "w": width, "h": height,
                "prompt": getattr(body, "cover_prompt", "") or "",
                "model": "agent-upload", "url": ""}
    cover_url = getattr(body, "cover_url", None)
    if cover_url is not None and cover_url.strip():
        return {"data": None, "mime": "", "w": None, "h": None,
                "prompt": "", "model": "", "url": cover_url.strip()}
    if getattr(body, "generate_cover", False):
        result = generate_cover(title,
                                getattr(body, "cover_ratio", DEFAULT_RATIO),
                                getattr(body, "cover_with_title", False),
                                getattr(body, "cover_extra", "") or "",
                                getattr(body, "cover_seed", None))
        return {"data": base64.b64decode(result["image_base64"]), "mime": result["mime"],
                "w": result["width"], "h": result["height"], "prompt": result["prompt"],
                "model": result.get("model", ""), "url": ""}
    return None
