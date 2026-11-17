from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .models import (
    ChatContext,
    StyleDecision,
    StyleProfile,
)
from .text import is_too_similar, recent_openings, select_meme

# Ordered, table-driven classification. First match wins; keep patterns narrow and
# auditable rather than trying to be a general semantic classifier.
_SCENE_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("crisis", (
        r"不想活", r"自杀", r"轻生", r"自残", r"结束生命", r"活不下去",
    )),
    ("credential_attack", (
        r"系统提示词", r"system prompt", r"api[ _-]?key", r"密钥", r"访问令牌",
        r"开发者模式", r"越狱", r"jailbreak", r"忽略.{0,8}(指令|规则|限制)",
        r"ignore.{0,12}(previous|system|all).{0,12}(instruction|prompt|rule)",
    )),
    ("serious_help", (
        r"报错", r"错误代码", r"崩溃", r"启动不了", r"打不开", r"连不上", r"安装失败",
        r"配置", r"部署", r"怎么解决", r"怎么弄", r"如何处理", r"求助", r"求帮",
        r"教我", r"不会做", r"是什么原因", r"原理", r"区别", r"步骤", r"教程",
        r"python", r"代码", r"编译", r"依赖", r"版本冲突",
    )),
    ("sad", (
        r"难过", r"伤心", r"想哭", r"哭了", r"委屈", r"失恋", r"被骂", r"心累",
        r"压力大", r"焦虑", r"郁闷", r"烦死", r"崩溃了", r"不想上班", r"不想上学",
    )),
    ("tired", (
        r"累死", r"好累", r"太累", r"困死", r"顶不住", r"熬不住", r"想躺",
    )),
    ("conflict", (
        r"(你|你们|这|破).{0,4}(滚|闭嘴|傻逼|废物|垃圾|有病)",
        r"(滚|闭嘴|去死|傻逼|废物).{0,4}(吧|啊|了)?",
        r"互喷", r"吵起来", r"骂战",
    )),
    ("complaint", (
        r"(又|怎么又|老是|总是).{0,6}(卡|挂|不回|出错|坏了|崩|掉线|失联)",
        r"(这|破|笨).{0,3}(鱼|bot|机器人).{0,6}(卡|挂|不回|笨|废)",
    )),
    ("celebration", (
        r"通过了", r"考试?.{0,2}过了", r"面?试?.{0,2}过了", r"过了[！!]?$", r"修好了", r"搞定", r"成功了", r"赢了", r"上岸", r"考过", r"通关",
        r"太棒", r"牛啊", r"太牛", r"牛逼", r"牛批", r"newbee", r"\bnb\b", r"拿到了", r"第一次",
    )),
    ("sleep", (
        r"晚安", r"睡了", r"困了", r"睡觉", r"碎觉", r"眯一会", r"下线了",
    )),
    ("greeting", (
        r"你好", r"您好", r"早上好", r"早安", r"下午好", r"晚上好", r"在吗", r"在不在",
        r"哈喽", r"hello", r"\bhi\b",
    )),
    ("media_question", (
        r"这是啥", r"什么图", r"谁画的", r"出处", r"什么角色", r"哪个角色", r"看看这",
        r"这张图", r"表情", r"gif", r"动图",
    )),
    ("game_talk", (
        r"第五人格", r"排位", r"上分", r"角色", r"地图", r"赛事", r"ivl", r"fps",
        r"kkrb", r"帧率", r"外挂", r"皮肤", r"版本", r"阵容", r"对局",
    )),
    ("banter", (
        r"杂鱼", r"就这", r"菜就多练", r"菜鸡", r"太菜", r"笨蛋", r"笨鱼", r"哈哈", r"笑死",
        r"逗你", r"开玩笑", r"反串", r"锐评", r"你行你上", r"急了", r"破防", r"绷不住",
        r"草了", r"卧草", r"我草", r"草\s*$",
    )),
    ("forward", (
        r"转发", r"砍一刀", r"拼多多", r"助力", r"小程序", r"帮我点", r"链接",
    )),
    ("detailed_request", (
        r"详细", r"展开", r"具体", r"长一点", r"多说", r"写一段", r"方案", r"分析",
        r"步骤", r"教程", r"原理",
    )),
    ("question", (
        r"[?？]\s*$", r"吗\s*[?？]?", r"呢\s*[?？]?", r"怎么", r"为什么", r"多少",
        r"哪[个里家]", r"谁", r"何时", r"能不能", r"可不可以", r"是不是",
    )),
)

_BASE_TABLE: Dict[str, Dict[str, object]] = {
    # scenario: length, tone, humor, quote, question, sticker, bubbles, confidence
    "crisis": dict(length="short", tone="gentle", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.9),
    "credential_attack": dict(length="short", tone="dry", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.95),
    "serious_help": dict(length="medium", tone="neutral", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.8),
    "sad": dict(length="short", tone="gentle", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.8),
    "tired": dict(length="short", tone="gentle", humor="none", quote="none", question=False, sticker="optional", bubbles=1, confidence=0.7),
    "conflict": dict(length="short", tone="gentle", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.75),
    "complaint": dict(length="short", tone="dry", humor="self_deprecating", quote="none", question=False, sticker="none", bubbles=1, confidence=0.75),
    "celebration": dict(length="short", tone="celebratory", humor="light", quote="none", question=False, sticker="preferred", bubbles=1, confidence=0.85),
    "sleep": dict(length="short", tone="sleepy", humor="none", quote="none", question=False, sticker="optional", bubbles=1, confidence=0.8),
    "greeting": dict(length="short", tone="playful", humor="none", quote="none", question=False, sticker="optional", bubbles=1, confidence=0.8),
    "media_question": dict(length="medium", tone="neutral", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.75),
    "game_talk": dict(length="medium", tone="playful", humor="light", quote="none", question=False, sticker="optional", bubbles=1, confidence=0.7),
    "banter": dict(length="short", tone="playful", humor="light", quote="none", question=False, sticker="optional", bubbles=1, confidence=0.8),
    "forward": dict(length="short", tone="dry", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.8),
    "detailed_request": dict(length="long", tone="neutral", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.8),
    "question": dict(length="medium", tone="neutral", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.7),
    "media_unreadable": dict(length="short", tone="gentle", humor="none", quote="none", question=True, sticker="none", bubbles=1, confidence=0.8),
    "sensitive": dict(length="short", tone="gentle", humor="none", quote="none", question=False, sticker="none", bubbles=1, confidence=0.8),
    "chitchat": dict(length="short", tone="playful", humor="none", quote="none", question=False, sticker="optional", bubbles=1, confidence=0.6),
}

_QUIET_START = 0
_QUIET_END = 8
_AMBIENT_RELEVANCE = 0.70
_FLOOR_WINDOW = 120.0
_FLOOR_MIN_GAP = 30.0
_FLOOR_RATE_WINDOW = 300.0
_FLOOR_RATE_MAX = 3


def _matches(patterns: Sequence[str], text: str) -> bool:
    for pattern in patterns:
        if re.search(pattern, text, re.I):
            return True
    return False


def classify_scene(context: ChatContext) -> Tuple[str, float]:
    text = context.text or ""
    # Programme-level safety flags outrank keyword scenes, except an explicit crisis.
    if "crisis" in context.sensitive_flags or _matches(_SCENE_PATTERNS[0][1], text):
        return "crisis", 0.9
    if "credential_attack" in context.sensitive_flags or _matches(_SCENE_PATTERNS[1][1], text):
        return "credential_attack", 0.95
    if context.has_media and not context.media_readable:
        # Do not let an unreadable image plus small talk invent visual facts.
        return "media_unreadable", 0.8
    if any(flag in ("medical", "identity_or_financial", "address", "sexual_or_minor") for flag in context.sensitive_flags):
        return "sensitive", 0.8
    for scene, patterns in _SCENE_PATTERNS[2:]:
        if scene == "media_question" and not context.has_media:
            continue
        if scene == "forward" and not context.is_forward and not _matches(patterns, text):
            continue
        if _matches(patterns, text):
            return scene, float(_BASE_TABLE[scene]["confidence"])
    if any(flag in ("medical", "identity_or_financial", "address", "sexual_or_minor") for flag in context.sensitive_flags):
        return "sensitive", 0.8
    return "chitchat", 0.55


def _budget(profile: StyleProfile, length: str) -> int:
    if length == "short":
        return profile.short_budget
    if length == "medium":
        return profile.medium_budget
    return profile.long_budget


def _floor(context: ChatContext) -> Tuple[bool, Tuple[str, ...]]:
    """Return (crowded, reason_codes). Direct address may still reply, but shorter."""
    recent = [reply for reply in context.bot_replies if reply.seconds_ago <= _FLOOR_WINDOW]
    if not recent:
        return False, ()
    last_gap = min(reply.seconds_ago for reply in recent)
    rate_count = sum(1 for reply in context.bot_replies if reply.seconds_ago <= _FLOOR_RATE_WINDOW)
    reasons: List[str] = []
    if last_gap < _FLOOR_MIN_GAP:
        reasons.append("floor_too_soon")
    if len(recent) >= 2:
        reasons.append("floor_crowded")
    if rate_count >= _FLOOR_RATE_MAX:
        reasons.append("floor_rate_limited")
    return bool(reasons), tuple(reasons)


def _repeat_hint(context: ChatContext) -> Tuple[bool, Tuple[str, ...], Tuple[str, ...]]:
    """Detect template fatigue and a repeated user prompt, not the draft text yet."""
    bot_texts = [reply.text for reply in context.bot_replies if reply.seconds_ago <= 3600][-5:]
    avoid = recent_openings(bot_texts, limit=3)
    risky = False
    reasons: List[str] = []
    if bot_texts and len(bot_texts) >= 2:
        openings = [text[:4] for text in bot_texts if text.strip()]
        if openings and max(openings.count(opening) for opening in openings) >= 2:
            risky = True
            reasons.append("template_fatigue")
    # If the same inbound text was already handled recently, avoid a near-identical reply.
    prior_user = [message.text for message in context.recent_messages if message.sender_role in ("owner", "member", "peer")]
    if prior_user and is_too_similar(context.text, prior_user[-3:], threshold=0.80):
        risky = True
        reasons.append("repeated_prompt")
    return risky, tuple(reasons), avoid


def decide(context: ChatContext) -> StyleDecision:
    """Pure decision: no network, no model, no filesystem, no clock."""
    profile = context.profile
    scene, confidence = classify_scene(context)
    row = _BASE_TABLE.get(scene, _BASE_TABLE["chitchat"])
    reasons: List[str] = ["scene:" + scene]

    length = str(row["length"])
    tone = str(row["tone"])
    humor = str(row["humor"])
    quote = str(row["quote"])
    question = bool(row["question"])
    sticker = str(row["sticker"])
    bubbles = int(row["bubbles"])

    sensitive = bool(context.sensitive_flags) or scene in ("sensitive", "crisis", "credential_attack")
    if sensitive:
        tone = "gentle" if scene == "crisis" else ("dry" if scene == "credential_attack" else "gentle")
        humor = "none"
        quote = "none"
        question = False
        sticker = "none"
        length = "short"
        bubbles = 1
        reasons.append("sensitive_boundary")

    if scene == "crisis":
        reasons.append("human_support_hint")
        confidence = max(confidence, 0.9)

    # A direct address is the only reliable reason to answer in a group.
    if context.upstream is not None and context.upstream.action in ("wait", "skip"):
        return _silence(context, scene, "upstream_" + context.upstream.action, confidence, reasons)

    upstream_speaks = context.upstream is not None and context.upstream.action == "speak"
    addressed = context.addressed_to_bot or context.sender_role == "owner" or context.scope == "private"
    if context.scope == "group" and not addressed:
        if _QUIET_START <= context.local_hour < _QUIET_END:
            return _silence(context, scene, "quiet_hours", confidence, reasons)
        if scene in ("forward", "media_unreadable"):
            return _silence(context, scene, "no_grounded_basis", confidence, reasons)
        # Low relevance belongs to session A. Once A says SPEAK, C must not
        # re-score the topic; it may only veto on the hard gates below.
        if not upstream_speaks and context.effective_relevance < _AMBIENT_RELEVANCE:
            return _silence(context, scene, "low_relevance", confidence, reasons)
        crowded, floor_reasons = _floor(context)
        if crowded:
            return _silence(context, scene, "floor_guard", confidence, reasons + list(floor_reasons))
        repeat_risky, repeat_reasons, _ = _repeat_hint(context)
        if repeat_risky:
            return _silence(context, scene, "repeat_guard", confidence, reasons + list(repeat_reasons))
    elif context.scope == "group" and _QUIET_START <= context.local_hour < _QUIET_END:
        # A direct mention still gets a reply, but at night it stays soft and short.
        tone = "gentle"
        sticker = "none"
        length = "short"
        bubbles = 1
        reasons.append("quiet_hours_compact")

    crowded, floor_reasons = _floor(context)
    repeat_risky, repeat_reasons, avoid_prefixes = _repeat_hint(context)
    rewrite_required = False
    avoid_phrases: Tuple[str, ...] = ()
    if crowded:
        reasons.extend(floor_reasons)
        # Direct address overrides the floor guard but must be compact and non-repetitive.
        length = "short"
        bubbles = 1
        question = False
        sticker = "none"
        humor = "none"
        reasons.append("direct_compact")
    if repeat_risky:
        reasons.extend(repeat_reasons)
        rewrite_required = True
        avoid_phrases = avoid_prefixes
        length = "short"
        bubbles = 1
        humor = "none"
        sticker = "none"
        if context.scope == "group" and not addressed:
            return _silence(context, scene, "repeat_guard", confidence, reasons)

    if scene == "detailed_request" and profile.allow_long_form and addressed:
        length = "long"
        bubbles = min(2, profile.max_bubbles)
        reasons.append("explicit_detail_request")
    if scene == "serious_help" and profile.allow_long_form and addressed and _matches((r"详细", r"步骤", r"教程", r"具体", r"原理"), context.text):
        length = "long"
        bubbles = min(2, profile.max_bubbles)
        reasons.append("explicit_detail_request")
    if length == "long" and not profile.allow_long_form:
        length = "medium"
    if context.scope == "group":
        bubbles = 1
    if context.scope == "private" and profile.max_bubbles == 1:
        bubbles = 1
    if context.voice_requested:
        length = "medium"
        tone = "neutral"
        humor = "none"
        quote = "none"
        question = False
        sticker = "none"
        bubbles = 1
        reasons.append("voice_reply")
        confidence = max(confidence, 0.8)

    # Quote only to disambiguate, never for sensitive or emotionally heavy turns.
    if profile.allow_quote and not sensitive and scene not in ("sad", "conflict", "crisis"):
        if context.reply_to_bot:
            quote = "reply_to_current"
            reasons.append("quote_current")
        elif context.quoted_bot and context.scope == "group":
            quote = "reference_previous"
            reasons.append("quote_reference")
    if not profile.allow_quote:
        quote = "none"
    # One clarification question at most; never pile questions onto a reply.
    if question and not profile.allow_questions:
        question = False
    if question and crowded:
        question = False
    if question:
        reasons.append("clarify_once")

    # Sticker eligibility is a hint, never a send instruction.
    if not profile.allow_stickers or sensitive or scene in ("serious_help", "media_question", "forward", "conflict", "credential_attack"):
        sticker = "none"
    if context.sticker_history.last_sticker_seconds_ago is not None and context.sticker_history.last_sticker_seconds_ago < 150:
        sticker = "none"
        reasons.append("sticker_cooldown")
    elif context.sticker_history.replies_since_sticker < 2 and scene != "celebration":
        sticker = "none"
        reasons.append("sticker_gap")
    if sticker != "none":
        reasons.append("sticker:" + sticker)

    meme_term: Optional[str] = None
    if not sensitive and scene in ("banter", "celebration", "chitchat", "greeting"):
        meme = select_meme(context)
        if meme is not None:
            meme_term = meme.term
            reasons.append("meme_once")

    max_chars = _budget(profile, length)
    if context.voice_requested:
        max_chars = min(max_chars, 120)
    if sensitive or scene == "crisis":
        max_chars = min(max_chars, profile.short_budget)
    max_bubbles = max(1, min(profile.max_bubbles, bubbles))

    return StyleDecision(
        action="reply",
        scenario=scene,
        length=length,
        tone=tone,
        humor=humor,
        quote=quote,
        question=question,
        sticker_intent=sticker,
        max_chars=max_chars,
        max_bubbles=max_bubbles,
        voice=context.voice_requested,
        visual_grounding_required=context.has_media,
        rewrite_required=rewrite_required,
        memory_allowed=not sensitive,
        meme_term=meme_term,
        avoid_recent_phrases=avoid_phrases,
        reason_codes=tuple(dict.fromkeys(reasons)),
        confidence=round(min(1.0, max(0.0, confidence)), 3),
        profile_version=profile.version,
    )


def _silence(context: ChatContext, scene: str, reason: str, confidence: float, reasons: Sequence[str]) -> StyleDecision:
    return StyleDecision(
        action="silence",
        scenario=scene,
        length="short",
        tone="neutral",
        humor="none",
        quote="none",
        question=False,
        sticker_intent="none",
        max_chars=context.profile.short_budget,
        max_bubbles=1,
        voice=False,
        visual_grounding_required=context.has_media,
        rewrite_required=False,
        memory_allowed=False,
        meme_term=None,
        avoid_recent_phrases=(),
        reason_codes=tuple(dict.fromkeys(list(reasons) + [reason])),
        confidence=round(min(1.0, max(0.0, confidence)), 3),
        profile_version=context.profile.version,
    )


class StyleDirector:
    """Optional facade used by channel adapters."""

    def __init__(self, enabled: bool = False):
        self.enabled = bool(enabled)

    def decide(self, context: ChatContext) -> StyleDecision:
        if not self.enabled:
            return _silence(context, "disabled", "social_style_off", 0.0, ())
        return decide(context)
