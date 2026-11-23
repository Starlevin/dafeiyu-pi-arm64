# -*- coding: utf-8 -*-
"""最小可审计的微信「发送目标一致性」守卫（纯逻辑，零网络/零凭据/零 UI 操作）。

设计依据：对真实 AT-SPI 暴露状态的**只读被动核验**（见 DELIVERY.md 所引
evidence/LIVE_FACTS.md 与 LATENCY.md，Pi 真机 134/134 采样，2026-11-14）：

* 会话行 ``list item`` 的 ``SELECTED`` 状态是唯一稳定、可独立读出的「当前会话」
  信号（134/134 样本恒有且仅有 1 个 SELECTED）。
* 会话标题（header 内首个 showing label）会**滞后于**选中行（实测 ≥1.34s 窗口），
  且 composer 的 ``get_name()`` 恰等于标题（同源副本，不是独立标识）。
* header 子树内可能存在 ``showing=False`` 且 ``ext=(0,0,0,0)``、name 等于目标名的
  **ghost label**（实测持续 20~37s）。真实 Pi 采样（raw_r09 / raw_r09b，136 样本）
  显示 ghost=1 时 45/46 次 ``sel != TARGET``（UI 已切走，本就被选中行判据拒绝），
  仅 1 次 title+sel 同为 TARGET 且 ghost=1。因此**不把 ghost 单独当阻断条件**，
  改为要求「header 内恰有 1 个 showing 的目标名 label」——这直接覆盖
  ghost 状态位抖动/遍历顺序变化导致的「两个 showing 标题」风险，且不会因一个
  惰性 DOM 残留节点长期压制正常群回复。稳定性比较使用每次采集的可见身份信号。
* 群 / 同名私聊在 AT-SPI 上**没有原生标识**：行名首行不带成员数后缀、行无子节点、
  ``parse_row_name().is_group`` 恒 False；`pi_bridge` 的 ``is_group`` 全部来自
  ``--group`` 配置断言。header 标题的 ``(N)`` 成员数后缀只是一个**启发式提示**
  （`group_is_open` 已隐含要求 ``raw_title != name``），不是群 ID；**屏幕外/滚动
  出去的同名会话无法被现有信号证明不存在**。本模块不宣称能区分，只做
  「唯一可见同名行 + 该行恰被选中 + 标题与选中行一致」的 fail-closed，并把
  scope 恒置 None / 不可证明的限制如实写进文档。

本模块只做**判定**，不做任何 UI 操作：所有观测由调用方注入 ``observe()`` 闭包，
因此可在没有 gi/AT-SPI 的机器上单测，也不与其它补丁形成 import 环。

fail-closed 语义：观测异常 / 字段缺失 / SELECTED 缺失或多余 / 标题与选中行背离 /
同名歧义 / header 标题不唯一 / 草稿非空且非我方 / 状态不明 → 一律 deny，绝不盲重试。
真正 @ 与非 @ 主动候选共用「身份守卫」；仅非 @ 主动候选额外走
``auto_reply_recheck``（评分期间新消息 → 静默丢弃），真 @ 不受此额外拦截。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

# group/private 现场证明不了：默认 off（不断言但也不放松其余判据）；
# probe = 给不出范围证据就拒绝（最严，通常用于隔离验证，不用于生产）。
SCOPE_MODE = (os.environ.get("DAFEIYU_WECHAT_TX_SCOPE") or "off").strip().lower()
if SCOPE_MODE not in ("off", "group", "private", "probe"):
    SCOPE_MODE = "off"

# 是否额外要求 header 标题带 (N) 成员数后缀作为「像群」的启发式提示。
# 默认 OFF：实测两个目标群都带后缀，但不能保证所有群/所有微信版本都带，
# 打开后可能误拦不带后缀的正常群；它只是提示，不是群身份证明。
REQUIRE_GROUP_HINT = os.environ.get("DAFEIYU_WECHAT_TX_GROUP_HINT") == "1"

# 一次完整观测必须给出的键；缺任意一个即 proof_incomplete（fail-closed）。
_REQUIRED_PROOF = (
    "ok", "app_ok", "title", "raw_title", "group_suffix",
    "showing_target",
    "selected", "visible_matches", "selected_matches",
    "draft", "char_count", "composer_fp", "composer_editor_ok", "scope",
)

# 一次轻量观测（点击后 / 回车后）必须给出的键：只读 SELECTED + 唯一匹配，
# 不做 chat_title / composer / header 遍历（现场实测 9 行 SELECTED ≈15ms）。
_REQUIRED_LIGHT = ("ok", "app_ok", "selected", "visible_matches",
                   "selected_matches", "char_count")


class Verdict:
    __slots__ = ("allow", "reason", "phase")

    def __init__(self, allow, reason, phase):
        self.allow = bool(allow)
        self.reason = reason
        self.phase = phase

    def __bool__(self):
        return self.allow

    def __repr__(self):
        return "Verdict(%s, allow=%r, reason=%r)" % (
            self.phase, self.allow, self.reason)


def ok(reason, phase):
    return Verdict(True, reason, phase)


def deny(reason, phase):
    return Verdict(False, reason, phase)


def norm_target(text):
    """与 pi_bridge.norm_title 同口径（剥掉群标题末尾的成员数后缀）。

    刻意不 import pi_bridge：守卫要在没有 gi/AT-SPI 的机器上可单测，且避免
    与其它补丁形成 import 环；口径一致性由控制流测试守护。
    """
    text = (text or "").strip()
    if text.endswith(")") or text.endswith("）"):
        cut = max(text.rfind("("), text.rfind("（"))
        if cut > 0 and text[cut + 1:-1].isdigit():
            return text[:cut].strip()
    return text


def row_first_line(raw):
    """侧栏行名首行才是会话名；第 2/3 行是预览与时间。"""
    return (raw or "").split("\n", 1)[0].strip()


def declared_scope():
    if SCOPE_MODE in ("group", "private"):
        return SCOPE_MODE
    return None


def _signature(sig):
    """把一次观测压成可比较元组，用于「连续 N 次一致」的稳定性判定。"""
    return (
        norm_target(sig.get("title") or ""),
        tuple(norm_target(row_first_line(n)) for n in (sig.get("selected") or [])),
        sig.get("visible_matches"),
        sig.get("selected_matches"),
        sig.get("showing_target"),
        sig.get("scope"),
    )


def identity_verdict(sig, target, phase, *, require_group_hint=False):
    """「目标身份」证明：标题 + 唯一 SELECTED + 唯一可见匹配 + header 标题唯一
    + scope 不可证明。不含草稿判定，供媒体/表情内部检查与清草稿证明复用。

    AT-SPI 无法证明群/同名私聊、也无法证明屏幕外不存在同名会话；本函数只给出
    「在当前可见 UI 上，目标名唯一且被选中且标题一致」这一可验证事实，绝不宣称
    能区分群与同名私聊。
    """
    def d(reason):
        return deny(reason, phase)

    if not isinstance(sig, dict) or not sig.get("ok"):
        return d((sig or {}).get("reason") or "observe_failed")
    if [k for k in _REQUIRED_PROOF if k not in sig]:
        return d("proof_incomplete")
    if not sig.get("app_ok"):
        return d("client_unavailable")

    # 1) 标题：必须存在且归一后恰为目标；单独一条标题不足以放行。
    title = norm_target(sig.get("title") or "")
    if not title:
        return d("title_missing")
    if title != target:
        return d("title_mismatch")

    # 2) header 标题唯一性：恰有 1 个 showing 的目标名 label。
    #    ghost（showing=False 的残留同名 label）不再是独立阻断条件：chat_title
    #    本就按 showing 过滤，惰性残留不可被选中；真正风险是「出现两个 showing
    #    同名 label」或「一个都没有」，这里直接覆盖，避免长期压制正常回复。
    if sig.get("showing_target") != 1:
        return d("header_title_ambiguous")

    # 3) 选中行：唯一稳定信号，必须恰好 1 个且就是目标。
    selected = [norm_target(row_first_line(n))
                for n in (sig.get("selected") or [])]
    if len(selected) != 1:
        return d("no_selected_row" if not selected else "multiple_selected_rows")
    if selected[0] != target:
        return d("selected_row_mismatch")

    # 4) 侧栏 / 标题一致 + 同名歧义：可见同名行必须恰好 1 个，且被选中的
    #    那一行必须正是这个唯一匹配。注意：只统计**可见**行；屏幕外/滚动出去的
    #    同名会话无法证明不存在，这是本方案的既定限制（见 DELIVERY.md）。
    visible = sig.get("visible_matches")
    if visible != 1:
        return d("no_visible_match" if not visible else "ambiguous_same_name")
    if sig.get("selected_matches") != 1:
        return d("selected_not_unique_match")

    # 5) 范围：AT-SPI 证明不了群/私聊，scope 恒 None；不拿配置断言冒充现场证据。
    scope = sig.get("scope")
    if scope is not None:
        return d("scope_unexpectedly_claimed")
    if SCOPE_MODE == "probe":
        return d("identity_scope_unproven")

    # 6) 可选的「像群」启发式：header 标题带 (N) 成员数后缀。
    #    只是提示不是证明；默认关闭，避免误拦不带后缀的正常群。
    if require_group_hint or REQUIRE_GROUP_HINT:
        if not sig.get("group_suffix"):
            return d("group_identity_hint_absent")
    return ok("identity_verified", phase)


def light_verdict(sig, target, phase):
    """轻量身份证明：只读 SELECTED + 唯一匹配，不读标题/composer/header。
    用于点击后、回车后的即时复核（现场 9 行 SELECTED ≈15ms）。"""
    def d(reason):
        return deny(reason, phase)

    if not isinstance(sig, dict) or not sig.get("ok"):
        return d((sig or {}).get("reason") or "observe_failed")
    if [k for k in _REQUIRED_LIGHT if k not in sig]:
        return d("proof_incomplete")
    if not sig.get("app_ok"):
        return d("client_unavailable")
    selected = [norm_target(row_first_line(n))
                for n in (sig.get("selected") or [])]
    if len(selected) != 1:
        return d("no_selected_row" if not selected else "multiple_selected_rows")
    if selected[0] != target:
        return d("selected_row_mismatch")
    if sig.get("visible_matches") != 1 or sig.get("selected_matches") != 1:
        return d("ambiguous_same_name")
    return ok("identity_light_verified", phase)


def _decide(sig, target, phase, require_empty, expected_draft, expected_scope,
            require_group_hint=False):
    """单次完整观测 → Verdict = 身份证明 + 草稿判定。"""
    verdict = identity_verdict(sig, target, phase,
                               require_group_hint=require_group_hint)
    if not verdict.allow:
        return verdict

    def d(reason):
        return deny(reason, phase)

    # 草稿：发送前必须空；按 Return 前必须恰好等于我方草稿（防夹带/覆盖）。
    # 注意：基线 send_reply 本来就有「草稿为空 + 输入回读一致」检查，这里是
    # **叠加**独立身份守卫，不是补一个原本不存在的草稿检查。
    draft = sig.get("draft") or ""
    if require_empty:
        if sig.get("char_count"):
            return d("composer_not_empty")
        if draft:
            return d("draft_foreign")
    else:
        if expected_draft is None:
            return d("draft_check_missing")
        if draft != expected_draft:
            return d("draft_mismatch_or_foreign")
    return ok("identity_verified", phase)


def may_clear_draft(sig, target, expected_draft, own_fp):
    """能否安全清空输入框？**只有四项全部证明成立才允许**。

    1. 当前会话身份仍完全对齐（identity_verdict，含唯一 SELECTED / 标题 /
       header 标题唯一 / 唯一可见匹配）；
    2. 输入框内容**恰好**仍是我方写入的草稿（未被用户修改、非他人草稿）；
    3. 字符数与草稿长度一致；
    4. 即将被清空的输入框**指纹**（role/name/rect）与写入时一致（未被替换），
       且其 editable text 接口仍可用（编辑器未失效）。

    任何一项证明不了 → deny，调用方**绝不操作输入框**，只报告安全失败。
    这避免了「切群后清空了用户在别群刚打的草稿」「用户已改草稿却被清掉」
    「输入框被替换 / 编辑器失效仍盲写」等误伤。
    """
    identity = identity_verdict(sig, target, "clear_proof")
    if not identity.allow:
        return identity
    if sig.get("draft") != expected_draft:
        return deny("draft_not_exclusively_ours", "clear_proof")
    if sig.get("char_count") != len(expected_draft or ""):
        return deny("draft_length_mismatch", "clear_proof")
    if own_fp is None or sig.get("composer_fp") != own_fp:
        return deny("composer_replaced", "clear_proof")
    if not sig.get("composer_editor_ok"):
        return deny("composer_editor_unavailable", "clear_proof")
    return ok("safe_to_clear", "clear_proof")


class TxGuard:
    """一次发送一个实例。四个门 + before_media；每个门返回 Verdict。

    * before_send   点 composer 之前   （完整观测）
    * after_click   点击之后立刻       （轻量观测，抓「点击导致切群」）
    * before_return 按 Return 之前     （完整观测，含草稿精确比对）
    * after_return  按完之后           （轻量观测，确认未切走，否则 unconfirmed）
    * before_media  图片/表情路径同口径（完整观测）

    轻量门只证明 SELECTED 未漂移；真正「最终提交前的完整身份校验」由紧邻
    Return 的 before_return 承担（SELECTED 是现场唯一稳定可信信号）。
    """

    def __init__(self, target, observe, *, observe_light=None, clock=None,
                 sleep=None, samples=1, gap_s=0.0, expected_scope=None,
                 require_group_hint=False):
        self.target = target
        self.observe = observe
        self.observe_light = observe_light
        self.clock = time.monotonic if clock is None else clock
        self.sleep = time.sleep if sleep is None else sleep
        self.samples = max(1, int(samples))
        self.gap_s = max(0.0, float(gap_s))
        self.expected_scope = expected_scope
        self.require_group_hint = require_group_hint
        self.stats = {"observations": 0, "light_observations": 0,
                      "denials": 0, "phases": []}

    def _observe(self):
        self.stats["observations"] += 1
        try:
            sig = self.observe()
        except Exception as error:
            return None, "observe_exception:" + type(error).__name__
        if not isinstance(sig, dict):
            return None, "observe_not_dict"
        if not sig.get("ok"):
            return None, sig.get("reason") or "observe_failed"
        return sig, None

    def _observe_light(self):
        self.stats["light_observations"] += 1
        try:
            sig = self.observe_light()
        except Exception as error:
            return None, "observe_light_exception:" + type(error).__name__
        if not isinstance(sig, dict):
            return None, "observe_light_not_dict"
        if not sig.get("ok"):
            return None, sig.get("reason") or "observe_light_failed"
        return sig, None

    def _log(self, verdict):
        self.stats["phases"].append((verdict.phase, verdict.reason))
        if not verdict.allow:
            self.stats["denials"] += 1
        return verdict

    def _verify(self, phase, *, require_empty, expected_draft, samples):
        baseline = None
        for index in range(samples):
            sig, error = self._observe()
            if error:
                return self._log(deny(error, phase))
            verdict = _decide(sig, self.target, phase, require_empty,
                              expected_draft, self.expected_scope,
                              self.require_group_hint)
            if not verdict.allow:
                return self._log(verdict)
            current = _signature(sig)
            if baseline is None:
                baseline = current
            elif current != baseline:
                return self._log(deny("identity_unstable", phase))
            if index + 1 < samples and self.gap_s:
                self.sleep(self.gap_s)
        return self._log(ok("identity_verified", phase))

    def _verify_light(self, phase, character_count):
        if self.observe_light is None:
            return self._verify(phase, require_empty=True,
                                expected_draft=None, samples=1)
        sig, error = self._observe_light()
        if error:
            return self._log(deny(error, phase))
        verdict = light_verdict(sig, self.target, phase)
        if not verdict.allow:
            return self._log(verdict)
        if character_count:
            return self._log(deny("composer_not_empty", phase))
        return self._log(ok("identity_light_verified", phase))

    def before_send(self):
        return self._verify("before_send", require_empty=True,
                            expected_draft=None, samples=self.samples)

    def after_click(self, character_count):
        return self._verify_light("after_click", character_count)

    def before_return(self, draft):
        return self._verify("before_return", require_empty=False,
                            expected_draft=draft, samples=self.samples)

    def after_return(self, character_count):
        return self._verify_light("after_return", character_count)

    def before_media(self):
        return self._verify("before_media", require_empty=True,
                            expected_draft=None, samples=self.samples)

    def before_media_send(self, own_fp=None):
        """选完附件点击发送按钮前：重新完整核验目标身份（可选校验原输入框指纹未变）。"""
        sig, error = self._observe()
        if error:
            return self._log(deny(error, "before_media_send"))
        verdict = identity_verdict(sig, self.target, "before_media_send",
                                   require_group_hint=self.require_group_hint)
        if not verdict.allow:
            return self._log(verdict)
        if own_fp is not None and sig.get("composer_fp") != own_fp:
            return self._log(deny("composer_replaced", "before_media_send"))
        if own_fp is not None and not sig.get("composer_editor_ok"):
            return self._log(deny("composer_editor_unavailable", "before_media_send"))
        return self._log(verdict)

    def clear_media_proof(self, own_fp, expected_draft=None):
        """仅在同时证明身份、原输入框指纹、独占草稿与附件身份时才允许清理。

        AT-SPI 对图片附件通常只暴露通用占位符 ``\\ufffc``（不含文件哈希或路径），
        无法可靠证明草稿完全由本程序独占且未夹带用户内容；因此当
        ``media_identity_proven`` 不为 True、草稿为空或与 ``expected_draft`` 不一致时，
        一律 deny，由调用方保留未知草稿并报告，绝不冒险发退格/删除键。
        """
        sig, error = self._observe()
        if error:
            return self._log(deny(error, "clear_media_proof"))
        identity = identity_verdict(sig, self.target, "clear_media_proof",
                                    require_group_hint=self.require_group_hint)
        if not identity.allow:
            return self._log(identity)
        if own_fp is None or sig.get("composer_fp") != own_fp:
            return self._log(deny("composer_replaced", "clear_media_proof"))
        if not sig.get("composer_editor_ok"):
            return self._log(deny("composer_editor_unavailable", "clear_media_proof"))
        draft = sig.get("draft") or ""
        if not draft:
            return self._log(deny("draft_empty", "clear_media_proof"))
        if sig.get("char_count") != len(draft):
            return self._log(deny("draft_length_mismatch", "clear_media_proof"))
        if expected_draft is None or draft != expected_draft:
            return self._log(deny("media_draft_not_exclusively_ours", "clear_media_proof"))
        if not sig.get("media_identity_proven"):
            return self._log(deny("media_identity_unproven", "clear_media_proof"))
        return self._log(ok("safe_to_clear", "clear_media_proof"))

    def clear_proof(self, expected_draft, own_fp):
        """取一次新的完整观测，判定能否安全清空输入框（不自己操作 UI）。"""
        sig, error = self._observe()
        if error:
            return self._log(deny(error, "clear_proof"))
        return self._log(may_clear_draft(sig, self.target, expected_draft, own_fp))


# ---------------------------------------------------------------------------
# 非 @ 主动候选：评分期间「未被新 incoming 超越」的证明
# ---------------------------------------------------------------------------
def row_digests(rows):
    """可见行快照：只存 SHA-256 哈希，不存正文 / 成员 / 凭据。"""
    return tuple(hashlib.sha256((text or "").encode("utf-8")).hexdigest()
                 for text, _ in rows)


def rowset_trace(target, digests, index):
    """候选身份：目标 + 候选及其前两行哈希 + 序号（全程无正文）。"""
    window = list(digests[max(0, index - 2):index + 1])
    payload = json.dumps([target, window, index], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_auto_context(target, rows, index):
    """候选时刻建立上下文；索引非法时给出 trace=None，复验必然 deny。"""
    digests = row_digests(rows)
    if not 0 <= index < len(digests):
        return {"target": target, "rows": digests, "index": index, "trace": None}
    return {"target": target, "rows": digests, "index": index,
            "trace": rowset_trace(target, digests, index)}


def auto_reply_recheck(cand, current_rows):
    """投递前证明候选未被新 incoming 超越；**证明不了就 deny（静默丢弃）**。

    只允许一种可证明情形：可见行快照与候选时刻**完全一致**，且候选身份可由
    当前行复算一致。任何新增行 / 视口滚动 / 重排 / 读不出 / 身份变化 → deny。
    真 @ 从不调用本函数（真 @ 的 auto_ctx 为 None）。
    """
    if not isinstance(cand, dict):
        return deny("auto_ctx_missing", "auto_recheck")
    cand_rows = cand.get("rows")
    index = cand.get("index")
    trace = cand.get("trace")
    if (not isinstance(cand_rows, (tuple, list)) or not isinstance(index, int)
            or not trace or not isinstance(cand.get("target"), str)):
        return deny("auto_ctx_incomplete", "auto_recheck")
    if current_rows is None:
        return deny("rows_unreadable", "auto_recheck")
    current = row_digests(current_rows)
    if index < 0 or index >= len(cand_rows) or index >= len(current):
        return deny("candidate_not_locatable", "auto_recheck")
    if rowset_trace(cand["target"], current, index) != trace:
        return deny("candidate_identity_changed", "auto_recheck")
    if tuple(current) != tuple(cand_rows):
        return deny("new_rows_or_viewport_shift", "auto_recheck")
    return ok("candidate_not_overtaken", "auto_recheck")
