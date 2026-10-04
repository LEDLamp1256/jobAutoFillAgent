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
    public var displayName: String? = nil

    public var id: String { runId }
    public var taskHistoryLabel: String {
        "\(queuedCount) application task\(queuedCount == 1 ? "" : "s") created"
    }

    public func historyLabel(applications: [ApplicationDTO]) -> String {
        if let displayName, !displayName.isEmpty { return displayName }
        if let application = applications.first(where: { $0.runId == id }) {
            let title = application.title.trimmingCharacters(in: .whitespacesAndNewlines)
            let company = application.company.trimmingCharacters(in: .whitespacesAndNewlines)
            if !title.isEmpty && title != "Direct URL application" {
                return company.isEmpty ? title : "\(title) · \(company)"
            }
        }
        return requestedSources.contains("direct_url") ? "Direct application" :
            requestedSources.joined(separator: ", ")
    }
}

public struct ApplicationDTO: Codable, Identifiable, Equatable, Sendable {
    public let taskId: String
    public let runId: String
    public let listingId: String
    public let company: String
    public let title: String
    public let sourceUrl: String?
    public let liveUrl: String?
    public let classification: String?
    public let status: String
    public let ownership: String
    public let currentPageOrStep: String?
    public let attentionCategory: String?
    public let blocker: String?
    public var blockerLabel: String? = nil
    public let failureReason: String?
    public let failureEvents: [FailureEventDTO]?
    public let recoveryAvailable: Bool?
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

public struct FailureEventDTO: Codable, Equatable, Sendable {
    public let reason: String
    public let stage: String
    public let category: String
    public let detail: String
    public let mode: String
    public let occurredAt: String
}

public struct ArchiveApplicationDTO: Codable, Equatable, Sendable {
    public let taskId: String
    public let archived: Bool
    public let removedFromQueue: Bool
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
    public let requiredness: String?
    public let category: String?
    public var reportGroup: String? = nil
    public var isBlocking: Bool? = nil
    public var requiresUserAction: Bool? = nil
    public var isCurrentPage: Bool? = nil

    public var id: String { entryId }
    public var undoDialogTitle: String { "Undo Mark Resolved" }
    public var undoDialogMessage: String {
        "Mark “\(visibleLabel)” as unresolved again? Your earlier action stays in the history. The employer page is not changed."
    }
    public var isTechnicalDetail: Bool { category == "technical" }
    public var requirednessLabel: String? {
        switch requiredness {
        case "required": "Required"
        case "optional": "Optional"
        case "unknown": "Requiredness Unknown"
        default: nil
        }
    }
    public var outcomeLabel: String {
        if isBlocking == true { return "Needs your input" }
        if reviewState == "pending" { return "Needs review" }
        switch action {
        case "human_resolved", "human_resolved_prior_page": return "Resolved manually"
        case "manual_complete": return "Manually completed"
        case "filled_text", "selected_option", "selected_toggle", "uploaded_document":
            return "Filled and verified"
        case "confirmed_trusted": return "Filled and verified"
        case "optional_skipped": return "Unanswered"
        default: return action.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    public var sourceLabel: String? {
        switch provenance {
        case "verified_profile": return "Candidate profile"
        case "deterministic": return "Saved answer"
        case "human_provided": return "Human provided"
        default: return nil
        }
    }
    public var reasonLabel: String? {
        guard let reason else { return nil }
        if reason == "unsupported_control" {
            return "Unsupported for automation"
        }
        if reason == "human_attested" {
            // Keep the human attestation distinct from browser verification.
            return "You marked this resolved · not verified on the employer page"
        }
        if reason == "human_entered_value" || reason == "optional_without_trusted_answer" {
            return nil
        }
        return reason.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

public struct ReportDTO: Codable, Equatable, Sendable {
    public let taskId: String
    public let entries: [ReportEntryDTO]
    public let pendingReviewCount: Int?
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

public struct FieldDiagnosticDTO: Codable, Sendable {
    public let taskId: String
    public let capturedAt: String?
    public let diagnosticGeneration: String?
    public let fieldCount: Int
    public let truncated: Bool
    public let fields: [FieldDiagnosticRowDTO]
    public let discoverySummary: DiscoverySummaryDTO?
}

public struct DiscoverySummaryDTO: Codable, Sendable {
    public let normalizedFieldCount: Int
    public let accessibilityQuestionCount: Int
    public let domRecoveredFieldCount: Int
    public let rawActionableCount: Int
    public let ignoredActionableCount: Int
    public let ignoredReasons: [String: Int]
    public let navigationCount: Int
    public let sectionActionCount: Int
    public let truncated: Bool
}

public struct FieldDiagnosticRowDTO: Codable, Sendable {
    public let label: String?
    public let questionIdentity: String
    public let section: String?
    public let recordContext: String?
    public let rawRole: String?
    public let interactionKind: String
    public let requiredness: String
    public let currentValuePresent: Bool
    public let requiredEvidence: String?
    public let currentAnswer: String?
    public let answerEvidence: String?
    public let placeholder: Bool
    public let satisfied: Bool
    public let automationCapability: String
    public let resolverCapability: String
    public let blocking: Bool
    public let reportGroup: String?
    public let manualResolution: String?
    public let discoverySource: String?
    public let stateConflict: Bool?
}

public enum ControlOperation: Equatable, Sendable {
    case startApplicationURL(String)
    case listRuns
    case getRun(String)
    case listApplications(String?)
    case getApplication(String)
    case listAttentionRequired(String?)
    case getApplicationReport(String)
    case diagnoseCurrentFields(String)
    case getNarrativeEntries(String)
    case resumeApplication(String)
    case openApplication(String)
    case recoverApplication(String)
    case archiveApplication(String)
    case bringWindowToFront(String)
    case reviewReportEntry(String)
    case resolveField(String)
    case undoFieldResolution(String)
    case markFinalReviewChecked(String)
    case recordSubmission(String)
    case replaceNarrative(entryID: String, text: String)

    public var method: String {
        switch self {
        case .startApplicationURL: "start_application_url"
        case .listRuns: "list_runs"
        case .getRun: "get_run"
        case .listApplications: "list_applications"
        case .getApplication: "get_application"
        case .listAttentionRequired: "list_attention_required"
        case .getApplicationReport: "get_application_report"
        case .diagnoseCurrentFields: "diagnose_current_fields"
        case .getNarrativeEntries: "get_narrative_entries"
        case .resumeApplication: "resume_application"
        case .openApplication: "open_application"
        case .recoverApplication: "recover_application"
        case .archiveApplication: "archive_application"
        case .bringWindowToFront: "bring_window_to_front"
        case .reviewReportEntry: "review_report_entry"
        case .resolveField: "resolve_field"
        case .undoFieldResolution: "undo_field_resolution"
        case .markFinalReviewChecked: "mark_final_review_checked"
        case .recordSubmission: "record_submission"
        case .replaceNarrative: "replace_narrative"
        }
    }

    private var parameters: [String: String] {
        switch self {
        case .startApplicationURL(let url): ["url": url]
        case .listRuns: [:]
        case .getRun(let id): ["run_id": id]
        case .listApplications(let id), .listAttentionRequired(let id):
            id.map { ["run_id": $0] } ?? [:]
        case .getApplication(let id), .getApplicationReport(let id),
             .diagnoseCurrentFields(let id),
             .getNarrativeEntries(let id), .resumeApplication(let id),
             .openApplication(let id), .recoverApplication(let id),
             .archiveApplication(let id),
             .bringWindowToFront(let id), .markFinalReviewChecked(let id),
             .recordSubmission(let id):
            ["task_id": id]
        case .reviewReportEntry(let id), .resolveField(let id), .undoFieldResolution(let id):
            ["entry_id": id]
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
