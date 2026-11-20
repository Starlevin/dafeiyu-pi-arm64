import Foundation
import Testing
@testable import BotCore

private let peer = PersonaProfile(displayName: "大肥鱼", relationship: .peer, maxCharacters: 60,
                                  ticBudget: 1, emojiBudget: 1, allowTeasing: true)

@Suite struct PersonaGuardTests {
    @Test func naturalShortChatPasses() {
        let result = PersonaGuard.check("在呢，刚看到。组队还缺人吗？", profile: peer, scenario: .casual)
        #expect(result.passed)
    }

    @Test func claimsOfBeingHumanAreBlocked() {
        let result = PersonaGuard.check("我是真人，不是AI。", profile: peer, scenario: .identity)
        #expect(result.blocking)
        #expect(result.hits.contains { $0.code == .claimsHuman })
    }

    @Test func aiDisclaimerIsBlocked() {
        let result = PersonaGuard.check("作为一个AI助手，我可以帮你分析。", profile: peer, scenario: .help)
        #expect(result.blocking)
        #expect(result.hits.contains { $0.code == .aiDisclaimer })
    }

    @Test func honestAIIdentityIsAllowed() {
        #expect(PersonaGuard.check("我是AI驱动的鲸鱼娘，不是真人。", profile: peer, scenario: .identity).passed)
        #expect(PersonaGuard.check("我是真人吗？你猜。", profile: peer, scenario: .banter).passed)
    }

    @Test func fabricatedSharedExperienceNeedsEvidence() {
        let without = PersonaGuard.check("我记得我们上次一起打排位。", profile: peer, scenario: .casual,
                                         context: PersonaCheckContext(hasMemoryEvidence: false))
        #expect(without.blocking)
        #expect(without.hits.contains { $0.code == .fabricatedSharedExperience })
        let with = PersonaGuard.check("上次你说的那个阵容，我记下了。", profile: peer, scenario: .casual,
                                      context: PersonaCheckContext(hasMemoryEvidence: true))
        #expect(!with.blocking)
        // The runtime must derive evidence from the actual recalled text, not from the
        // existence of a memory book: overlapping content licenses the recall, unrelated
        // memory must not license "we played ranked together last time".
        #expect(PersonaGuard.hasSupportingMemoryEvidence(
            "上次你说的那个阵容，我记下了。",
            memory: "上次你说想试双辅助阵容"))
        #expect(!PersonaGuard.hasSupportingMemoryEvidence(
            "我记得我们上次一起打排位。",
            memory: "上次你说想试双辅助阵容"))
        #expect(!PersonaGuard.hasSupportingMemoryEvidence(
            "我记得我们上次一起打排位。",
            memory: ""))
    }

    @Test func romanticClaimsAndOwnerAddressAreBlockedForPeers() {
        #expect(PersonaGuard.check("做我女朋友吧。", profile: peer, scenario: .banter).blocking)
        let owner = PersonaGuard.check("主人，今天的活干完了。", profile: peer, scenario: .casual)
        #expect(owner.blocking)
        #expect(owner.hits.contains { $0.code == .overFamiliarAddress })
    }

    @Test func ownerMayUseOwnerAddressButNotRomance() {
        var ownerProfile = peer; ownerProfile.relationship = .owner
        #expect(!PersonaGuard.check("主人，我先去睡了。", profile: ownerProfile, scenario: .casual).blocking)
        #expect(PersonaGuard.check("你是我老婆。", profile: ownerProfile, scenario: .banter).blocking)
    }

    @Test func ticBudgetIsEnforced() {
        let result = PersonaGuard.check("喵喵喵，这也太典了典了典了。", profile: peer, scenario: .banter)
        #expect(!result.blocking)
        #expect(result.hits.contains { $0.code == .repeatedTic })
    }

    @Test func markdownStageDirectionsAndServiceToneAreCaught() {
        #expect(PersonaGuard.check("- 第一点\n- 第二点", profile: peer, scenario: .help).rewriteRequired)
        #expect(PersonaGuard.check("（笑）你说得对。", profile: peer, scenario: .casual).rewriteRequired)
        #expect(PersonaGuard.check("有什么可以帮您的吗？", profile: peer, scenario: .help).rewriteRequired)
        #expect(PersonaGuard.check("首先，你要先登录。其次，再重启。", profile: peer, scenario: .help).rewriteRequired)
    }

    @Test func fabricatedSystemCompetenceIsBlocked() {
        let result = PersonaGuard.check("我已经帮你重启了服务器。", profile: peer, scenario: .help)
        #expect(result.blocking)
        #expect(result.hits.contains { $0.code == .fabricatedCompetence })
    }

    @Test func exactDuplicateOfRecentReplyIsFlagged() {
        let result = PersonaGuard.check("在呢。", profile: peer, scenario: .greeting,
                                        context: PersonaCheckContext(recentReplies: ["在呢。"]))
        #expect(result.hits.contains { $0.code == .repeatedReply })
    }

    @Test func overlongReplyIsFlaggedAndTrimKeepsSentence() {
        let long = "这个问题要先看网络，再看配置，最后看服务日志，不要一上来就重装，也不要拿重启当万能药，先确认现象和报错，再一步步缩小范围。"
        var narrow = peer; narrow.maxCharacters = 30
        let result = PersonaGuard.check(long, profile: narrow, scenario: .help)
        #expect(result.hits.contains { $0.code == .excessiveLength })
        #expect(PersonaGuard.trim(long, to: 30).count <= 31)
    }

    @Test func stylePlansVaryBySeedAndScenario() {
        let a = PersonaGuard.stylePlan(profile: peer, scenario: .casual, seed: 1)
        let b = PersonaGuard.stylePlan(profile: peer, scenario: .casual, seed: 2)
        #expect(a.directive != b.directive)
        #expect(a.minCharacters <= a.maxCharacters)
        #expect(a.directive.contains("不写括号动作"))
        #expect(PersonaGuard.stylePlan(profile: peer, scenario: .help, seed: 3).ticBudget == 0)
    }
}
