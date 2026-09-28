import Foundation

/// Presentation only: retain both persisted entries and pair a unique replacement with its draft.
public struct NarrativePresentation: Identifiable, Equatable, Sendable {
    public let current: ReportEntryDTO
    public let history: [ReportEntryDTO]

    public var id: String { current.id }

    public static func items(from entries: [ReportEntryDTO]) -> [Self] {
        let narratives = entries.filter { $0.kind == "narrative" }
        let grouped = Dictionary(grouping: narratives, by: NarrativeKey.init)
        let positions = Dictionary(uniqueKeysWithValues: narratives.enumerated().map { ($0.element.id, $0.offset) })
        var historyByCurrent: [String: ReportEntryDTO] = [:]
        var historicalIDs = Set<String>()

        for group in grouped.values {
            let drafts = group.filter { $0.provenance == "ai_draft_review" && $0.reviewState == "approved" }
            let replacements = group.filter {
                $0.provenance == "human_provided" && $0.action == "replaced" && $0.reviewState == "approved"
            }
            guard drafts.count == 1, replacements.count == 1,
                  let draft = drafts.first, let replacement = replacements.first,
                  let draftPosition = positions[draft.id], let replacementPosition = positions[replacement.id],
                  draftPosition < replacementPosition else { continue }
            historyByCurrent[replacement.id] = draft
            historicalIDs.insert(draft.id)
        }

        return narratives.filter { !historicalIDs.contains($0.id) }.map {
            Self(current: $0, history: historyByCurrent[$0.id].map { [$0] } ?? [])
        }
    }
}

private struct NarrativeKey: Hashable {
    let taskId: String
    let pageOrStep: String?
    let visibleLabel: String
    let semanticKey: String?

    init(_ entry: ReportEntryDTO) {
        taskId = entry.taskId
        pageOrStep = entry.pageOrStep
        visibleLabel = entry.visibleLabel
        semanticKey = entry.semanticKey
    }
}
