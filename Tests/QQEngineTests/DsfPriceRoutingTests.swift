import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import WeChatAIBot
import BotCore

private final class MockDsfProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) throws -> (Int, Data))?
    nonisolated(unsafe) static var requestedPaths: [String] = []

    static func reset(_ fn: (@Sendable (URLRequest) throws -> (Int, Data))?) {
        lock.lock()
        handler = fn
        requestedPaths = []
        lock.unlock()
    }

    static var paths: [String] {
        lock.lock()
        defer { lock.unlock() }
        return requestedPaths
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        let fn: (@Sendable (URLRequest) throws -> (Int, Data))?
        Self.lock.lock()
        Self.requestedPaths.append(request.url?.path ?? "")
        fn = Self.handler
        Self.lock.unlock()

        guard let fn else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        do {
            let (status, data) = try fn(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
}

@Suite(.serialized) struct DsfPriceRoutingTests {
    private func mockSession() -> URLSession {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [MockDsfProtocol.self]
        cfg.urlCache = nil
        cfg.httpCookieStorage = nil
        return URLSession(configuration: cfg)
    }

    private func fixtureNow() -> Date {
        DsfPriceTool.isoDate("2026-11-15T04:00:00Z")!
    }

    private func marketData(fetchedAt: String = "2026-11-15T03:45:00Z",
                            capturedFrom: String = "2026-11-04T17:30:46Z",
                            capturedTo: String = "2026-11-15T03:40:00Z",
                            prices: [String: Any]? = nil,
                            schemaVersion: Int = 2) -> Data {
        let obj: [String: Any] = [
            "schemaVersion": schemaVersion,
            "priceVersion": fetchedAt,
            "currency": "gameCurrency",
            "source": [
                "kind": "orzice-work-api",
                "dataVersion": "dv-fixture",
                "capturedFrom": capturedFrom,
                "capturedTo": capturedTo,
                "fetchedAt": fetchedAt
            ],
            "prices": prices ?? [
                "13020000167": 2562,
                "37280300001": 1100,
                "37280400001": 1200,
                "37280500001": 1300,
                "90000000001": 888
            ],
            "availability": [
                "13020000590": [
                    "status": "unavailable",
                    "reason": "providerCatalogUnavailable",
                    "lastKnownPrice": 131598,
                    "observedAt": "2026-11-15T03:40:00Z"
                ]
            ]
        ]
        return try! JSONSerialization.data(withJSONObject: obj)
    }

    private func itemsData(schemaVersion: Int = 3) -> Data {
        let obj: [String: Any] = [
            "schemaVersion": schemaVersion,
            "kind": "itemLocalization",
            "locale": "zh-CN",
            "items": [
                "13020000167": ".357口径左轮长枪管",
                "13020000590": "M249H.A.V.K链锯套件",
                // 三重歧义（3 个 ID 都在交易行）——回归验证无 AMBIGUOUS:AMBIGUOUS: 双重前缀
                "37280300001": ".300 BLK",
                "37280400001": ".300 BLK",
                "37280500001": ".300 BLK",
                // (未命名配置) 类场景：4 个同名 ID，仅 1 个在交易行
                "90000000001": "(未命名配置)",
                "90000000002": "(未命名配置)",
                "90000000003": "(未命名配置)",
                "90000000004": "(未命名配置)"
            ]
        ]
        return try! JSONSerialization.data(withJSONObject: obj)
    }

    private func standardHandler(fetchedAt: String = "2026-11-15T03:45:00Z",
                                 capturedFrom: String = "2026-11-04T17:30:46Z",
                                 capturedTo: String? = nil) -> (@Sendable (URLRequest) throws -> (Int, Data)) {
        let effectiveTo = capturedTo ?? fetchedAt
        let m = marketData(fetchedAt: fetchedAt, capturedFrom: capturedFrom, capturedTo: effectiveTo)
        let i = itemsData()
        return { req in
            switch req.url?.path {
            case "/data/v3/economy/market-prices.json": return (200, m)
            case "/data/v3/locales/zh-CN/items.json": return (200, i)
            default: return (404, Data())
            }
        }
    }

    @Test func priceOutsideIntegerRangeIsRejectedWithoutTrapping() async {
        let market = marketData(prices: ["13020000167": UInt64(Int.max) + 1])
        let items = itemsData()
        MockDsfProtocol.reset { request in
            (200, request.url == DsfPriceTool.marketURL ? market : items)
        }
        let session = mockSession()
        defer { session.invalidateAndCancel() }
        await #expect(throws: URLError.self) {
            try await DsfPriceTool.loadSnapshot(session: session, now: fixtureNow())
        }
    }

    @Test func disabledOrPrivateChatNeverTouchesNetwork() async {
        MockDsfProtocol.reset(standardHandler())
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        let disabled = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: false), session: session)
        #expect(await disabled.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow()) == nil)

        let enabled = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true), session: session)
        #expect(await enabled.fragment(message: "三角洲 13020000167 多少钱", isGroup: false, scope: "p1", now: fixtureNow()) == nil)
        #expect(MockDsfProtocol.paths.isEmpty)
    }

    @Test func coldStartNameQueryLoadsNamesAndCachesSnapshot() async {
        MockDsfProtocol.reset(standardHandler())
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        let coord = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, maxAgeMinutes: 60), session: session)
        let first = await coord.fragment(message: "三角洲 .357口径左轮长枪管 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(first != nil)
        #expect(first!.contains("2562"))
        #expect(first!.contains("2026-11-04T17:30:46Z ~ 2026-11-15T03:45:00Z"))
        #expect(first!.contains("非实时"))
        #expect(MockDsfProtocol.paths.count == 2)

        // 60 秒内第二次查询复用快照，不再发网络请求
        let second = await coord.fragment(message: "三角洲 13020000590 多少钱", isGroup: true, scope: "g1",
                                          now: fixtureNow().addingTimeInterval(10))
        #expect(second != nil)
        #expect(second!.contains("不可标价"))
        #expect(second!.contains("131598"))
        #expect(second!.contains("不是现价"))
        #expect(MockDsfProtocol.paths.count == 2)
    }

    @Test func singleTradedDuplicateResolvesWhileMultiTradedStaysAmbiguousWithoutDoublePrefix() async {
        MockDsfProtocol.reset(standardHandler())
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        let coord = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, maxAgeMinutes: 60), session: session)
        let single = await coord.fragment(message: "三角洲 (未命名配置) 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(single != nil)
        #expect(single!.contains("888"))
        #expect(single!.contains("90000000001"))

        let multi = await coord.fragment(message: "三角洲 .300 BLK 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(multi != nil)
        #expect(multi!.contains("无法唯一确定"))
        #expect(multi!.contains("37280300001"))
        #expect(multi!.contains("37280400001"))
        #expect(multi!.contains("37280500001"))
        #expect(!multi!.contains("AMBIGUOUS:"))
        #expect(!multi!.contains("当前快照报价"))
    }

    @Test func staleSnapshotFailsSafelyOnlyOnMatchedItemNotGeneralChatter() async {
        MockDsfProtocol.reset(standardHandler(fetchedAt: "2026-11-15T01:00:00Z", capturedTo: "2026-11-15T00:55:00Z"))
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        let coord = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, maxAgeMinutes: 60), session: session)
        // 泛聊没有具体物品：即使快照已过期也不误报过期
        let chatter = await coord.fragment(message: "三角洲 这个赛季物价好高啊", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(chatter == nil)

        // 命中具体物品：给出诚实过期提示 + 采集区间，绝不含价格数字
        let stale = await coord.fragment(message: "三角洲 .357口径左轮长枪管 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(stale != nil)
        #expect(stale!.contains("数据已过期"))
        #expect(stale!.contains("2026-11-04T17:30:46Z ~ 2026-11-15T00:55:00Z"))
        #expect(!stale!.contains("2562"))
        #expect(!stale!.contains("当前快照报价"))
    }

    @Test func timeoutAndFutureClockAndBadSchemaFailSafely() async {
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        MockDsfProtocol.reset { _ in throw URLError(.timedOut) }
        let c1 = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true), session: session)
        let timedOut = await c1.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(timedOut != nil)
        #expect(timedOut!.contains("请求超时"))
        #expect(!timedOut!.contains("当前快照报价"))

        MockDsfProtocol.reset(standardHandler(fetchedAt: "2026-11-15T08:00:00Z")) // 超前 4 小时
        let c2 = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true), session: session)
        let futureClock = await c2.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(futureClock != nil)
        #expect(futureClock!.contains("响应结构不可信"))

        let badMarket = marketData(schemaVersion: 99)
        let goodItems = itemsData()
        MockDsfProtocol.reset { req in
            req.url?.path == "/data/v3/economy/market-prices.json" ? (200, badMarket) : (200, goodItems)
        }
        let c3 = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true), session: session)
        let badSchema = await c3.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(badSchema != nil)
        #expect(badSchema!.contains("响应结构不可信"))
    }

    @Test func dailyBudgetStopsFurtherQueries() async {
        MockDsfProtocol.reset(standardHandler())
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        let coord = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, perChatDaily: 2), session: session)
        #expect(await coord.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow()) != nil)
        #expect(await coord.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow()) != nil)
        #expect(await coord.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow()) == nil)
    }

    @Test func rejectsBooleanPricesInMarketPayload() async {
        let bad = marketData(prices: ["13020000167": true])
        let items = itemsData()
        MockDsfProtocol.reset { req in
            req.url?.path == "/data/v3/economy/market-prices.json" ? (200, bad) : (200, items)
        }
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        let coord = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true), session: session)
        let out = await coord.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g1", now: fixtureNow())
        #expect(out != nil)
        #expect(out!.contains("响应结构不可信"))
        #expect(!out!.contains("当前快照报价"))
    }

    @Test func captureIntervalValidationAndLastBudgetBehavior() async {
        let session = mockSession()
        defer { session.invalidateAndCancel() }

        // 1) 新 fetchedAt 包装旧 capturedTo（3 小时前）-> 仍判过期且不含价格
        MockDsfProtocol.reset(standardHandler(fetchedAt: "2026-11-15T03:55:00Z",
                                              capturedFrom: "2026-11-14T10:00:00Z",
                                              capturedTo: "2026-11-15T00:50:00Z"))
        let cOldCap = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, maxAgeMinutes: 60), session: session)
        let oldCap = await cOldCap.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g-oldcap", now: fixtureNow())
        #expect(oldCap != nil)
        #expect(oldCap!.contains("数据已过期"))
        #expect(oldCap!.contains("旧采集数据不会因重新抓取而变成新行情"))
        #expect(!oldCap!.contains("2562"))

        // 2) 缺失 / 倒序 / 超前区间均 fail-closed 且 promptFragment 不虚构「落在②区间内」
        for (label, fromS, toS) in [
            ("missing", "", ""),
            ("reversed", "2026-11-15T03:50:00Z", "2026-11-15T02:00:00Z"),
            ("future", "2026-11-15T03:00:00Z", "2026-11-15T06:00:00Z"),
        ] {
            MockDsfProtocol.reset(standardHandler(fetchedAt: "2026-11-15T03:55:00Z",
                                                  capturedFrom: fromS,
                                                  capturedTo: toS))
            let coord = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, maxAgeMinutes: 60), session: session)
            let res = await coord.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "g-\(label)", now: fixtureNow())
            #expect(res != nil)
            #expect(res!.contains("响应结构不可信"))
            #expect(!res!.contains("2562"))

            let badSnap = DsfPriceTool.Snapshot(
                priceVersion: "2026-11-15T03:55:00Z", dataVersion: "dv",
                capturedFrom: fromS, capturedTo: toS,
                fetchedAt: "2026-11-15T03:55:00Z",
                prices: ["13020000167": 2562], availability: [:],
                fetchedLocallyAt: fixtureNow(), itemNames: ["测试": "13020000167"], bytes: 100
            )
            let frag = DsfPriceTool.promptFragment(
                request: DsfPriceRequest(itemID: "13020000167", matchedBy: "directID"),
                snapshot: badSnap, now: fixtureNow(), maxAgeMinutes: 60
            )
            #expect(frag.contains("无可靠区间"))
            #expect(!frag.contains("落在②"))
        }

        // 3) 剩余 1 个额度时：过期、超时与正常成功各可诚实输出一次且不双扣
        MockDsfProtocol.reset(standardHandler(fetchedAt: "2026-11-15T01:00:00Z", capturedTo: "2026-11-15T00:55:00Z"))
        let cLastStale = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, maxAgeMinutes: 60, perChatDaily: 1), session: session)
        let s1 = await cLastStale.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "ls", now: fixtureNow())
        #expect(s1 != nil && s1!.contains("数据已过期"))
        #expect(await cLastStale.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "ls", now: fixtureNow()) == nil)

        MockDsfProtocol.reset { _ in throw URLError(.timedOut) }
        let cLastTimeout = DsfPriceTool.Coordinator(config: DsfPriceConfig(enabled: true, perChatDaily: 1), session: session)
        let t1 = await cLastTimeout.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "lt", now: fixtureNow())
        #expect(t1 != nil && t1!.contains("请求超时"))
        #expect(await cLastTimeout.fragment(message: "三角洲 13020000167 多少钱", isGroup: true, scope: "lt", now: fixtureNow()) == nil)
    }
}
