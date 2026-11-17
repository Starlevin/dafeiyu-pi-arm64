import Foundation

/// Local pipeline timing. The final QQ milestone is OneBot acceptance.
struct QQReplyTiming: Sendable {
    let trace = UUID()
    let detected: ContinuousClock.Instant
    let group: Bool
    let proactive: Bool
    let queued = ContinuousClock.now
    var generationStart: ContinuousClock.Instant?
    var modelStart: ContinuousClock.Instant?
    var modelEnd: ContinuousClock.Instant?
    var submitStart: ContinuousClock.Instant?
    var accepted: ContinuousClock.Instant?

    private func milliseconds(_ start: ContinuousClock.Instant?, _ end: ContinuousClock.Instant?) -> String {
        guard let start, let end else { return "-" }
        let duration = start.duration(to: end).components
        return String(duration.seconds * 1000 + duration.attoseconds / 1_000_000_000_000_000)
    }

    var logLine: String {
        let mode = group ? (proactive ? "group_proactive" : "group_direct") : "private"
        return "[qq_reply_timing] trace=\(trace) mode=\(mode)"
            + " detected_to_queue_ms=\(milliseconds(detected, queued))"
            + " queue_to_worker_ms=\(milliseconds(queued, generationStart))"
            + " model_ms=\(milliseconds(modelStart, modelEnd))"
            + " submit_to_accepted_ms=\(milliseconds(submitStart, accepted))"
            + " detected_to_accepted_ms=\(milliseconds(detected, accepted))"
            + " accepted=\(accepted != nil)"
    }
}
