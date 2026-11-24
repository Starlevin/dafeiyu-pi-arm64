import Testing
@testable import BotCore

struct PublicGroupQuestionTests {
    @Test func acceptsConcreteQuestionsToTheWholeGroup() {
        let accepted = [
            "大家知道怎么把 PNG 放到一个文件夹吗？",
            "有人能帮我看看这个报错怎么解决吗",
            "各位大佬，这个编译错误怎么解决？",
            "请问大家有谁用过这个库吗？",
            "群里的朋友知道哪里能下载这个工具吗",
            "有没有人知道 Python 依赖冲突怎么处理？",
            "Does anyone know how to put a PNG into a folder?",
            "Can someone help me figure out this build error?",
            " guys any ideas on fixing this crash? ",
            "大家知道怎么把png放到一个文件夹吗?", // halfwidth, no spaces
            "大家知道厂长开会是什么意思", // no question mark
            "有没有人知道厂长开会这个梗哪来的" // no question mark
        ]
        for text in accepted {
            #expect(PublicGroupQuestion.matches(text), "should accept: \(text)")
        }
    }

    @Test func identityVTerminologyRequiresAnExplicitCollectiveQuestion() {
        #expect(PublicGroupQuestion.matches("各位，第五人格什么时候该压机？"))
        #expect(!PublicGroupQuestion.matches("这把压机压得真及时"))
        #expect(!PublicGroupQuestion.matches("压机"))
        #expect(!PublicGroupQuestion.matches("请问大家觉得压机这个梗怎么样？"))
    }

    @Test func rejectsGreetingsAndEmptyProbes() {
        let rejected = [
            "", "   ", "有人吗", "有人在不", "在吗", "在不在", "有没有人",
            "大家好", "大家", "各位", "hi", "hello", "test", "ping", "anyone?",
            "有人吗？", "有人在吗？", "大家在线吗", "hello everyone"
        ]
        for text in rejected {
            #expect(!PublicGroupQuestion.matches(text), "should reject: \(text)")
        }
    }

    @Test func rejectsDirectMentionAndReplyOrForwardMarkers() {
        let rejected = [
            "@小明 你觉得这个报错怎么解决？",
            "回复 小明：这个报错怎么解决？",
            "答：大家知道怎么把 PNG 放到一个文件夹吗？",
            "Re: 有人知道这个怎么弄吗？",
            "转发消息：有人能帮我看看这个报错吗",
            "转发 大家知道怎么把 PNG 放到一个文件夹吗？",
            "引用消息 有人知道怎么修复吗",
            "[CQ:at,qq=123] 谁会处理这个问题？",
            ">>> 有人知道怎么编译吗？",
            "聊天记录里的那个报错有人能看看吗？",
            "@All 这个怎么处理？"
        ]
        for text in rejected {
            #expect(!PublicGroupQuestion.matches(text), "should reject: \(text)")
        }
    }

    @Test func rejectsCommandsLinksAndMediaPlaceholders() {
        let rejected = [
            "/help 大家知道怎么用吗？",
            "!run 谁会处理这个报错？",
            "> 大家知道怎么编译吗？",
            "执行命令 ls 有人知道结果吗？",
            "请问大家这个链接 http://example.com 怎么打开？",
            "谁懂这个 www.example.com 的问题？",
            "大家看看 [图片] 里面这个报错怎么修？",
            "有人知道 [视频] 里的软件叫什么吗？",
            "[图片] 大家知道这是啥吗",
            "[文件] 各位能看看吗"
        ]
        for text in rejected {
            #expect(!PublicGroupQuestion.matches(text), "should reject: \(text)")
        }
    }

    @Test func rejectsResolvedClosingRhetoricalAndSelfAnswered() {
        let rejected = [
            "谢谢大家，已经解决了", "搞定了各位", "问题已修复", "我自己搞定了",
            "答案是重装依赖", "原来看是这样配置", "其实解决方法就是重启",
            "方法如下：先装依赖", "不用了大家", "算了，我找到了",
            "selfAnswered: figured it out", "nevermind", "no longer need help",
            "这还用问吗？", "谁不知道怎么把 PNG 放到文件夹？", "大家觉得这个好看吗？",
            "你们觉得这样行吗？"
        ]
        for text in rejected {
            #expect(!PublicGroupQuestion.matches(text), "should reject: \(text)")
        }
    }

    @Test func rejectsQuestionsWithNoConcreteContent() {
        let rejected = [
            "大家知道吗？", "有人能帮忙吗？", "各位会吗？", "请问谁知道？",
            "大家知道怎么做吗？", "有人能帮我看看吗？", "Does anyone know?",
            "Can someone help?", "guys any ideas?", "大家知道这个吗？"
        ]
        for text in rejected {
            #expect(!PublicGroupQuestion.matches(text), "should reject: \(text)")
        }
    }

    @Test func addresseeIsRequiredAndNotSatisfiedByAQuestionAlone() {
        #expect(!PublicGroupQuestion.matches("这个报错怎么解决？"))
        #expect(!PublicGroupQuestion.matches("How do I fix this build error?"))
        #expect(PublicGroupQuestion.matches("大家知道这个报错怎么解决吗？"))
    }

    @Test func normalizationIsRobustForFullwidthAndSeparators() {
        #expect(PublicGroupQuestion.matches("大家知道怎么把PNG放到一个文件夹吗？"))
        #expect(PublicGroupQuestion.matches("大家知道怎么把PNG放到一个文件夹吗?"))
        #expect(PublicGroupQuestion.matches("各位，报错怎么解决？"))
        #expect(PublicGroupQuestion.matches("大家知道怎么把PNG放到一个文件夹吗？"))
    }

    @Test func matcherIsPureAndDeterministic() {
        let text = "大家知道怎么把 PNG 放到一个文件夹吗？"
        #expect(PublicGroupQuestion.matches(text) == PublicGroupQuestion.matches(text))
        #expect(!PublicGroupQuestion.matches(String(text.reversed()))) // no hidden state
    }

    @Test func acceptsNoPunctuationExplanationQuestionsButNotStatements() {
        #expect(PublicGroupQuestion.matches("大家知道厂长开会是什么意思"))
        #expect(PublicGroupQuestion.matches("有没有人知道厂长开会这个梗哪来的"))
        #expect(PublicGroupQuestion.matches("大家知道厂长开会是什么意思吗"))
        #expect(!PublicGroupQuestion.matches("厂长又开会了"))
        #expect(!PublicGroupQuestion.matches("厂长开会了"))
    }

    @Test func rejectsIncidentalGroupMentionFollowedByDirectedQuestion() {
        #expect(!PublicGroupQuestion.matches("我刚跟大家说过，小王你知道厂长开会是什么意思吗"))
        #expect(!PublicGroupQuestion.matches("我刚跟大家说过，小王知道压机是什么意思吗？"))
        #expect(!PublicGroupQuestion.matches("我刚跟大家说过,小王知道压机是什么意思吗?"))
        #expect(PublicGroupQuestion.matches("大家知道厂长开会是什么意思吗"))
    }

    @Test func clauseBreakersWithoutGroupAddresseeDoNotRejectQuestion() {
        #expect(PublicGroupQuestion.matches("各位，刚才那局打到一半断网了，压机到底是什么意思？"))
        #expect(PublicGroupQuestion.matches("各位,刚才那局打到一半断网了,压机到底是什么意思?"))
    }
}
