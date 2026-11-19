#!/usr/bin/env python3
"""微信非 @ 主动接话：从桥接本次 UI 可见 rows 构造有限、脱敏的同群上下文。

为什么需要这个模块（审核缺口 #2）：
- core 的 `store.messages(key, 24)` **不是**完整近期群对话：pi_bridge.handle_rows
  对冷却期间未入队的普通消息直接 `continue`，那些消息根本不会进 core store。
- 同一批 rows 里，候选 i 之后可能已经有后续群友回答；core 侧无从得知。
- 因此必须由桥接在**入队那一刻**把「本次 UI 可见」的有限上下文随 payload 传出，
  并标记候选是否已被后续可见行超越；core 侧据此保守静默。

约束（严格执行）：
- 只取候选**之前**的可见文本，最多 8 条，每条前缀 125 字；
- 脱敏 URL / 邮箱 / 长数字 / @提及（与 QQ QQProactiveDecision.context 同一语义）；
- 只在本次 payload 与内存中传递，**不写盘、不落 health 账本、不跨群、不记日志**；
- 拿不到上下文（rows 不可用 / 全为空）→ 返回空，core 侧保守静默；
- 不声称拿到了所有消息；这只是「本次 UI 可见窗口」。
"""
from __future__ import annotations

import re
from typing import List, Tuple

# 总窗口上限（含候选自身）：QQProactiveDecision.context 为 8 条聊天，但微信侧
# 必须确保「当前候选」一定在窗口里（judge 直接把 preContext 发模型，漏掉候选会
# 让模型对一条「不存在」的消息打分）。因此：最多 7 条前文 + 1 条候选 = 8 条。
WINDOW_TOTAL = 8
MAX_PRIOR = WINDOW_TOTAL - 1   # 候选之前的可见文本最多 7 条
MAX_CHARS = 125

# 与 QQProactiveDecision.context 相同的脱敏语义（URL / 邮箱、长数字、@提及）
_REDACT_URL = re.compile(
    r"https?://\S+|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE)
_REDACT_ID = re.compile(r"[0-9]{5,}|@[^\s，。！？,:]{1,40}")
_MEDIA_PLACEHOLDER = frozenset(
    ("图片", "[图片]", "Image", "[Image]", "表情", "[表情]", "[动画表情]", ""))


def scrub(text: str) -> str:
    """单条文本：脱敏 + 截断。不抛异常，非法输入返回空串。"""
    if not isinstance(text, str):
        return ""
    value = _REDACT_URL.sub("[省略]", text)
    value = _REDACT_ID.sub("[省略]", value)
    return value[:MAX_CHARS]


def visible_context(rows, index: int, side_fn=None) -> List[str]:
    """候选 index **之前**的可见行，严格最多 7 条、每条 125 字、脱敏、丢空/丢媒体占位。

    取**最靠近候选**的 7 条（最近优先），不是最旧的 7 条——避免「取满 8 条就 break」
    挑到最旧消息而漏掉相关近期上下文。候选本身由 `windowed_context` 单独追加，
    保证 judge 拿到的窗口一定包含待回复的当前消息。

    若提供 `side_fn(row)` 且返回 ``"outgoing"``，说明该条为机器人自己的最近回复：
    截断其更早的前文并把该回复标为 ``机器人: ...``，避免下游评分把已被机器人回答
    的旧问题与后续闲聊混在一起误判为未回答。

    rows 形如 `[(text, rect), ...]`（pi_bridge.handle_rows 的入参）。
    只在本函数内读取，不修改 rows，不做任何 I/O。
    """
    if not isinstance(rows, (list, tuple)) or not isinstance(index, int) or index < 0:
        return []
    # 第一遍：收集候选之前所有可见文本（有界回看，避免 O(n) 扫全历史）
    start = max(0, index - (MAX_PRIOR * 4))
    prior: List[str] = []
    for i in range(start, min(index, len(rows))):
        row = rows[i]
        try:
            raw = row[0]
        except (IndexError, TypeError):
            continue
        side = None
        if side_fn is not None:
            try:
                side = side_fn(row)
            except Exception:
                side = None
        raw_str = raw.strip() if isinstance(raw, str) else ""
        cleaned = scrub(raw_str) if raw_str else ""
        if side == "outgoing":
            if cleaned and cleaned.strip() not in _MEDIA_PLACEHOLDER:
                prior = [("机器人: " + cleaned)[:MAX_CHARS]]
            elif raw_str and raw_str in _MEDIA_PLACEHOLDER:
                prior = [("机器人: " + raw_str)[:MAX_CHARS]]
            else:
                prior = []
            continue
        if not isinstance(raw, str):
            continue
        if not cleaned or cleaned.strip() in _MEDIA_PLACEHOLDER:
            continue
        prior.append(cleaned)
    # 最近优先：只保留紧邻候选的 MAX_PRIOR 条（最旧多余部分丢弃）
    return prior[-MAX_PRIOR:]


def windowed_context(rows, index: int, candidate_text: str, side_fn=None) -> Tuple[List[str], bool]:
    """最终发给 judge 的窗口 = 最近最多 7 条前文 + 当前候选（候选必在最后）。

    返回 (窗口, 是否截断过前文)。窗口长度严格 ≤ WINDOW_TOTAL(8)：
    - 候选文本为空/不合法 → 返回 (前文, has_prior)；此时 judge 只凭前文保守静默。
    - 候选文本合法 → 当前消息一定出现在窗口末尾，模型不会漏掉待回复消息。
    - 前文超过 7 条 → 只保留最近的 7 条（has_prior=True 表示有更早的被丢弃，
      但不影响判定：窗口已经包含最相关的近期上下文 + 当前候选）。
    """
    prior = visible_context(rows, index, side_fn=side_fn)
    has_prior = len(prior) >= MAX_PRIOR
    cand = scrub(str(candidate_text or '').strip()) if isinstance(candidate_text, str) else ''
    if not cand:
        # 没有可识别的候选文本：只给前文，judge 据此保守静默（不含当前消息）。
        return prior, has_prior
    return prior + [cand], has_prior


def superseded(rows, index: int) -> bool:
    """候选之后是否还有**任何后续可见消息行**（含图片/表情/动画表情等占位行）。

    审核要求：「候选被后续群消息超越时放弃或评分含最新信息」。
    原实现排除了 ``_MEDIA_PLACEHOLDER``，会导致同批「旧文本候选 → 后续图片/表情」
    被误判为仍是最后一条，进而绕过与交付前 ``auto_reply_recheck`` 一致的行序守卫。
    此处保守判断：只要候选之后仍存在任何可见 UI 行（或参数越界/不可读），即标记 superseded；
    真 @ 不走 ``build_proven`` / ``superseded``，不受影响。
    """
    if (not isinstance(rows, (list, tuple)) or not isinstance(index, int)
            or index < 0 or index >= len(rows)):
        return True   # 拿不到或越界 → 保守视为已超越
    return index + 1 < len(rows)


def build(rows, index: int, candidate_text: str = '', side_fn=None) -> Tuple[List[str], bool]:
    """返回 (最近≤7前文+当前候选的有界脱敏窗口, 候选是否已被后续可见行超越)。

    candidate_text 为本次候选的可见文本（pi_bridge 在 payload 构造点已知），
    必须传入并保证出现在窗口末尾；缺失时退化为仅前文（judge 保守静默）。
    兼容旧调用方；新链路请用 build_proven（带显式窗口获取成功证明）。
    """
    ctx, _truncated = windowed_context(rows, index, candidate_text, side_fn=side_fn)
    return ctx, superseded(rows, index)


def build_proven(rows, index: int, candidate_text: str = '', side_fn=None) -> Tuple[List[str], bool, bool]:
    """返回 (窗口, 候选是否被后续可见行超越, 窗口获取是否成功且可信)。

    ★ WorkBuddy 复核缺口：可信桥接必须**显式证明**「本次确实读到了 UI 可见窗口」，
    才能把「合法仅一条候选 [current]」（可以评分）与「根本没读到 UI 上下文」
    （缺模块 / 异常 / 未提供窗口 → 必须静默）区分开。

    window_ok=True 的充要条件（全部满足）：
      - rows / index 合法且 index 指向本次候选行；
      - 候选文本非空且成功脱敏，且**出现在窗口末尾**（= 本条待回复消息）；
      - 窗口非空、长度 ≤ WINDOW_TOTAL(8)。
    任一不满足 → window_ok=False，并把 superseded 置 True（双重 fail-closed），
    由 judge 在追加任何文本之前先判 windowOk，直接静默。

    注意：superseded 与 window_ok 互相独立——即使窗口读取成功，只要候选之后
    还有可见文本行（可能已被回答），superseded 仍为 True → judge 静默。
    """
    if (not isinstance(rows, (list, tuple)) or not isinstance(index, int)
            or index < 0 or index >= len(rows)):
        return [], True, False
    ctx, _truncated = windowed_context(rows, index, candidate_text, side_fn=side_fn)
    cand = scrub(str(candidate_text or '').strip()) if isinstance(candidate_text, str) else ''
    over = superseded(rows, index)
    if (not cand or not ctx or ctx[-1] != cand
            or len(ctx) > WINDOW_TOTAL or not isinstance(ctx, list)):
        # 候选未被成功纳入窗口 → 不能证明「读到了当前消息」→ 静默。
        return ctx, True, False
    return ctx, over, True
