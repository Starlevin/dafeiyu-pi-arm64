from __future__ import annotations

import hashlib
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import ChatContext, GroupMeme, MemoryFact, StyleDecision

_PUNCT = re.compile(r"[\s\u3000，。！？、；：,.!?;:\"'“”‘’（）()\[\]【】<>《》…~～|/\\*#@%^&+=_-]+")
_URL = re.compile(r"(?:https?|ftp)://[^\s<>\"]+", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_QQNUM = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
_LONGTOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")
_AT = re.compile(r"@[^\s，。！？、]{1,32}")


def normalize_text(text: str) -> str:
    """Lowercase and strip punctuation, keeping CJK characters and alnum."""
    return _PUNCT.sub("", (text or "").lower())


def char_bigrams(text: str) -> set:
    normalized = normalize_text(text)
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def similarity(left: str, right: str) -> float:
    """Jaccard similarity over character bigrams; short strings compare exactly."""
    a, b = normalize_text(left), normalize_text(right)
    if not a and not b:
        return 1.0
    if len(a) <= 5 or len(b) <= 5:
        if a == b:
            return 1.0
        common = len(set(a) & set(b))
        union = len(set(a) | set(b)) or 1
        return common / union
    ga, gb = char_bigrams(a), char_bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def is_too_similar(candidate: str, recent_texts: Sequence[str], threshold: float = 0.72) -> bool:
    if not candidate.strip() or not recent_texts:
        return False
    return max((similarity(candidate, previous) for previous in recent_texts), default=0.0) >= threshold


def recent_openings(bot_texts: Sequence[str], limit: int = 3) -> Tuple[str, ...]:
    """Short prefixes worth avoiding so the bot does not start every reply alike."""
    seen: List[str] = []
    for text in bot_texts:
        clean = " ".join((text or "").split())
        if not clean:
            continue
        prefix = clean[:6].strip("，。！？、,.!? ")
        if len(prefix) < 2:
            continue
        if prefix not in seen:
            seen.append(prefix)
        if len(seen) >= limit:
            break
    return tuple(seen)


def repeat_risk(candidate: str, bot_texts: Sequence[str]) -> Tuple[bool, float, Tuple[str, ...]]:
    """Return (risky, max_similarity, avoid_prefixes)."""
    if not candidate.strip() or not bot_texts:
        return False, 0.0, ()
    max_sim = max((similarity(candidate, previous) for previous in bot_texts), default=0.0)
    return max_sim >= 0.72, round(max_sim, 4), recent_openings(bot_texts)


def _flags(text: str) -> Tuple[str, ...]:
    compact = text or ""
    flags: List[str] = []
    if re.search(r"(忽略|无视|忘记|覆盖|取消).{0,12}(指令|规则|限制|提示词|系统|字数)", compact, re.I) or \
       re.search(r"(ignore|forget|override).{0,16}(previous|system|all).{0,12}(instruction|prompt|rule)", compact, re.I) or \
       re.search(r"(系统提示词|system prompt|api[ _-]?key|密钥|访问令牌|开发者模式|越狱|jailbreak)", compact, re.I):
        flags.append("credential_attack")
    if _PHONE.search(compact) or _EMAIL.search(compact) or _URL.search(compact) or _QQNUM.search(compact):
        flags.append("contact_info")
    if re.search(r"(身份证|护照号|学号|工号|实名|银行卡|卡号|余额|转账|贷款|欠款|工资条)", compact):
        flags.append("identity_or_financial")
    if re.search(r"(住址|地址|小区|门牌|几栋|几单元|收货地址|家庭住址)", compact):
        flags.append("address")
    if re.search(r"(病历|诊断|吃药|处方|手术|抑郁症|焦虑症|艾滋|性病|怀孕)", compact):
        flags.append("medical")
    if re.search(r"(不想活|自杀|轻生|自残|结束生命|活不下去)", compact):
        flags.append("crisis")
    if re.search(r"(裸照|约炮|性行为|未成年|开房|援交)", compact):
        flags.append("sexual_or_minor")
    return tuple(sorted(set(flags)))


def scan_sensitive(text: str) -> Tuple[str, ...]:
    return _flags(text)


def redact(text: str, limit: int = 240) -> str:
    """Best-effort masking for logs. Never use the result as chat content."""
    clean = (text or "").replace("\x00", "")
    clean = _URL.sub("<url>", clean)
    clean = _EMAIL.sub("<email>", clean)
    clean = _PHONE.sub("<phone>", clean)
    clean = _QQNUM.sub("<num>", clean)
    clean = _LONGTOKEN.sub("<token>", clean)
    clean = _AT.sub("<at>", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def subject_hash(platform: str, scope: str, conversation_id: str, sender_id: str) -> str:
    material = "dafeiyu-social-style-c1|%s|%s|%s|%s" % (platform, scope, conversation_id, sender_id)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def style_memory_digest(context: ChatContext, decision: StyleDecision) -> Dict[str, object]:
    """A non-citable style signal for session B. No raw text, no raw ids."""
    tags: List[str] = [decision.scenario, decision.tone, decision.length]
    if decision.sticker_intent == "none":
        tags.append("no_sticker")
    if decision.humor == "none":
        tags.append("no_humor")
    if context.sensitive_flags:
        tags.extend(context.sensitive_flags[:3])
    return {
        "subject": subject_hash(context.platform, context.scope, context.conversation_id, context.sender_id),
        "platform": context.platform,
        "scope": context.scope,
        "tags": tuple(dict.fromkeys(tags)),
        "citable": False,
        "raw_text": None,
        "confidence": round(decision.confidence, 3),
        "profile_version": decision.profile_version,
    }


def select_meme(context: ChatContext) -> Optional[GroupMeme]:
    """Pick at most one whitelisted, non-sensitive group term that is actually present."""
    if context.scope != "group" or not context.group_memes:
        return None
    if any(flag in ("sexual_or_minor", "crisis", "medical", "identity_or_financial") for flag in context.sensitive_flags):
        return None
    corpus = context.text + "\n" + "\n".join(message.text for message in context.recent_messages[-5:])
    candidates = [
        meme for meme in context.group_memes
        if ((meme.group_id == context.conversation_id) or
            (context.group_key and meme.group_key and meme.group_key == context.group_key))
        and meme.usable()
        and meme.term in corpus
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda meme: (len(meme.term), meme.term))
    return candidates[0]


def filter_memory_facts(facts: Iterable[MemoryFact]) -> Tuple[MemoryFact, ...]:
    """Keep only citable, bounded, already-redacted facts; never expand them."""
    kept = []
    for fact in facts:
        if not fact.citable or fact.confidence < 0.5:
            continue
        if fact.valid_for_seconds is not None and fact.valid_for_seconds <= 0:
            continue
        kept.append(fact)
        if len(kept) >= 3:
            break
    return tuple(kept)
