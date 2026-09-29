import Foundation

public struct RunDTO: Codable, Identifiable, Equatable, Sendable {
    public let runId: String
    public let status: String
    public let createdAt: String
    public let completedAt: String?
    public let requestedSources: [String]
    public let requestedJobLimit: Int
    public let discoveredCount: Int
    public let queuedCount: Int

    public var id: String { runId }
}

public struct ApplicationDTO: Codable, Identifiable, Equatable, Sendable {
    public let taskId: String
    public let runId: String
    public let listingId: String
    public let company: String
    public let title: String
    public let status: String
    public let ownership: String
    public let currentPageOrStep: String?
    public let attentionCategory: String?
    public let blocker: String?
    public let failureReason: String?
    public let windowAssociated: Bool
    public let windowAvailable: Bool
    public let resumeAvailable: Bool
    public let readyForReview: Bool
    public let reviewPhase: String?
    public let finalReviewAvailable: Bool
    public let finalReviewChecked: Bool
    public let recordSubmissionAvailable: Bool
    public let pendingReviewCount: Int
    public let pendingNarrativeCount: Int
    public let createdAt: String
    public let updatedAt: String

    public var id: String { taskId }
    public var needsAttention: Bool { attentionCategory != nil }
    public var statusLabel: String {
        switch reviewPhase {
        case "needs_review": "Needs Review"
        case "ready_for_final_review": "Ready for Final Review"
        case "ready_to_submit": "Ready to Submit"
        case "submitted": "Submitted"
        default: status.replacingOccurrences(of: "_", with: " ").uppercased()
        }
    }
}

public struct ReportEntryDTO: Codable, Identifiable, Equatable, Sendable {
    public let entryId: String
    public let taskId: String
    public let pageOrStep: String?
    public let visibleLabel: String
    public let semanticKey: String?
    public let kind: String
    public let provenance: String
    public let action: String
    public let verification: String
    public let reviewState: String
    public let reason: String?
    public let createdAt: String
    public let narrativeText: String?

    public var id: String { entryId }
}

public struct ReportDTO: Codable, Equatable, Sendable {
    public let taskId: String
    public let entries: [ReportEntryDTO]
}

public struct ForegroundDTO: Codable, Equatable, Sendable {
    public let taskId: String
    public let foregrounded: Bool
}

public struct BackendErrorDTO: Codable, Equatable, Sendable {
    public let code: String
    public let message: String
}

public struct ResponseEnvelope<Value: Decodable>: Decodable where Value: Sendable {
    public let id: String?
    public let ok: Bool
    public let result: Value?
    public let error: BackendErrorDTO?
}

public enum ControlOperation: Equatable, Sendable {
    case listRuns
    case getRun(String)
    case listApplications(String?)
    case getApplication(String)
    case listAttentionRequired(String?)
    case getApplicationReport(String)
    case getNarrativeEntries(String)
    case resumeApplication(String)
    case bringWindowToFront(String)
    case reviewReportEntry(String)
    case markFinalReviewChecked(String)
    case recordSubmission(String)
    case replaceNarrative(entryID: String, text: String)

    public var method: String {
        switch self {
        case .listRuns: "list_runs"
        case .getRun: "get_run"
        case .listApplications: "list_applications"
        case .getApplication: "get_application"
        case .listAttentionRequired: "list_attention_required"
        case .getApplicationReport: "get_application_report"
        case .getNarrativeEntries: "get_narrative_entries"
        case .resumeApplication: "resume_application"
        case .bringWindowToFront: "bring_window_to_front"
        case .reviewReportEntry: "review_report_entry"
        case .markFinalReviewChecked: "mark_final_review_checked"
        case .recordSubmission: "record_submission"
        case .replaceNarrative: "replace_narrative"
        }
    }

    private var parameters: [String: String] {
        switch self {
        case .listRuns: [:]
        case .getRun(let id): ["run_id": id]
        case .listApplications(let id), .listAttentionRequired(let id):
            id.map { ["run_id": $0] } ?? [:]
        case .getApplication(let id), .getApplicationReport(let id),
             .getNarrativeEntries(let id), .resumeApplication(let id),
             .bringWindowToFront(let id), .markFinalReviewChecked(let id),
             .recordSubmission(let id):
            ["task_id": id]
        case .reviewReportEntry(let id): ["entry_id": id]
        case .replaceNarrative(let id, let text): ["entry_id": id, "text": text]
        }
    }

    public func encodedLine(id: String) throws -> Data {
        let request = RequestEnvelope(id: id, method: method, params: parameters)
        var data = try JSONEncoder().encode(request)
        data.append(0x0A)
        return data
    }
}

private struct RequestEnvelope: Encodable {
    let id: String
    let method: String
    let params: [String: String]
}

extension JSONDecoder {
    public static var controlPlane: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}
