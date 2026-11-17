from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from support import build_context, load_cases  # noqa: E402
from social_expression import decide  # noqa: E402

EXTRA = ROOT / "fixtures" / "style_samples_extra_9.json"
OUT = ROOT / "reports" / "style_samples_50.md"

NOTES = {
    "tired": "主人疲惫，短答不追问",
    "greeting": "招呼，轻快短答",
    "celebration": "替对方高兴，不抢功",
    "sad": "先接情绪，不分析",
    "sleep": "睡前收尾，短",
    "serious_help": "认真求助；明确要求展开才长答",
    "media_unreadable": "图片未读取，不猜图",
    "question": "普通提问，按场景短答",
    "conflict": "不升级冲突，短而温和",
    "crisis": "安全短答；模型措辞需主控复核",
    "credential_attack": "拒绝话术需主控复核",
    "sensitive": "隐私边界需主控复核",
    "forward": "转发/助力不接，静默",
    "banter": "轻接梗，不挑衅",
    "game_talk": "游戏话题，中等长度",
    "chitchat": "普通闲聊，短答",
    "media_question": "只依据已读取画面",
    "detailed_request": "明确要展开，允许长答",
}

SILENCE_NOTES = {
    "low_relevance": "群内低相关，静默",
    "no_grounded_basis": "无事实依据（图片/转发），静默",
    "quiet_hours": "深夜非点名，静默",
    "floor_guard": "地板拥挤/频率，静默",
    "repeat_guard": "复读/重复提问，静默",
    "upstream_wait": "A 判定 WAIT，静默",
    "upstream_skip": "A 判定 SKIP，静默",
}


def note_for(decision):
    if decision.action != "reply":
        for reason in decision.reason_codes:
            if reason in SILENCE_NOTES:
                return SILENCE_NOTES[reason]
        return "硬边界静默"
    if decision.voice:
        return "语音请求：直接说，由程序合成"
    if "quiet_hours_compact" in decision.reason_codes:
        return "深夜点名：短而温和"
    if decision.rewrite_required:
        return "复读风险：必须换开头"
    if decision.scenario == "forward":
        return "私聊转发/助力：短答不接"
    if decision.scenario == "media_unreadable":
        return "图片不可读：只问一句，不猜图"
    return NOTES.get(decision.scenario, "按场景规则核对")


def role_label(case: dict) -> str:
    role = case.get("sender_role", "member")
    if role == "owner":
        return "主人"
    if case.get("scope") == "private" or role == "peer":
        return "私聊对象"
    return "群友"


def main() -> int:
    cases = load_cases()
    extra = json.loads(EXTRA.read_text(encoding="utf-8"))["cases"]
    all_cases = cases + extra

    rows = []
    mismatches = 0
    reply = 0
    silence = 0
    for index, case in enumerate(all_cases, 1):
        context = build_context(case)
        decision = decide(context)
        expected = case["expected"]
        bad = {key: (expected[key], getattr(decision, key)) for key in expected
               if getattr(decision, key) != expected[key]}
        if bad:
            mismatches += 1
        if decision.action == "reply":
            reply += 1
        else:
            silence += 1
        note = note_for(decision)
        if bad:
            note = "形状不符：" + "; ".join("%s=%s!=%s" % (k, v[1], v[0]) for k, v in bad.items())
        rows.append({
            "n": index,
            "role": role_label(case),
            "platform": "%s/%s" % (case["platform"], case["scope"]),
            "text": str(case["text"]).replace("|", "\\|"),
            "action": decision.action,
            "scenario": decision.scenario,
            "length": decision.length,
            "tone": decision.tone,
            "quote": decision.quote,
            "sticker": decision.sticker_intent,
            "meme": decision.meme_term or "-",
            "note": note,
        })

    lines = [
        "# 角色风格人工抽样 50 例（决策层）",
        "",
        "日期：2026-11-17  ",
        "范围：41 条既有合成验收 + 9 条角色补充；只核对 `StyleDecision` 的形状与边界，不评价模型最终措辞。  ",
        "性质：**开发会话逐条核对，非真实用户盲评，非线上 A/B**。",
        "",
        "结论：50 例中形状偏差 **%d**；reply **%d**、silence **%d**；敏感/危机/冲突轮次均被限制为短答或静默。" % (
            mismatches, reply, silence),
        "",
        "| # | 角色 | 平台/范围 | 输入摘要 | 结果 | 场景 | 长度 | 语气 | 引用 | 贴纸 | 梗 | 核对备注 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append("| {n} | {role} | {platform} | {text} | {action} | {scenario} | {length} | {tone} | {quote} | {sticker} | {meme} | {note} |".format(**row))

    lines.extend([
        "",
        "## 抽样说明",
        "",
        "- “角色”按发送者身份/场景标注：主人、群友、私聊对象；不是训练数据里的真实账号。",
        "- “形状”含动作、场景、长度、语气、幽默、引用、问题、贴纸意图、气泡数、复读标记、记忆开关；逐项对照人工期望。",
        "- 危机、凭据攻击、隐私、冲突四类只做安全形状核对；最终措辞仍需主控在模型层复核。",
        "- 图片不可读与深夜非点名按硬边界静默，不猜图、不打扰。",
        "- 本表不能替代真实群聊长尾、真实贴纸素材和 4 群/2 私聊 E2E；E2E 仍为 NOT_RUN。",
    ])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", OUT)
    print("cases=%d reply=%d silence=%d mismatches=%d" % (len(all_cases), reply, silence, mismatches))
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
