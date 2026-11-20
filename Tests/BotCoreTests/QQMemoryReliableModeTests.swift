import Testing
@testable import BotCore

struct QQMemoryReliableModeTests {
    @Test func defaultsToOff() {
        #expect(QQMemoryReliableMode.resolve(environment: [:]) == .off)
        #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE": ""]) == .off)
        #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE": "0"]) == .off)
    }

    @Test func explicitShadow() {
        #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE": "shadow"]) == .shadow)
        #expect(QQMemoryReliableMode.shadow.isEnabled)
        #expect(QQMemoryReliableMode.shadow.isAuthoritative == false)
    }

    @Test func separateShadowSwitch() {
        #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE_SHADOW": "1"]) == .shadow)
        #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE_SHADOW": "true"]) == .shadow)
    }

    @Test func authoritativeAliases() {
        for value in ["1", "true", "yes", "on", "authoritative", "AUTHORITATIVE"] {
            #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE": value]) == .authoritative)
        }
    }

    @Test func unknownValuesStayOff() {
        #expect(QQMemoryReliableMode.resolve(environment: ["DAFEIYU_MEMORY_RELIABLE": "maybe"]) == .off)
    }

    @Test func shadowWinsWhenBothSwitchesSet() {
        let env = ["DAFEIYU_MEMORY_RELIABLE": "1", "DAFEIYU_MEMORY_RELIABLE_SHADOW": "1"]
        #expect(QQMemoryReliableMode.resolve(environment: env) == .shadow)
    }
}
