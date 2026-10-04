import Combine
import Foundation

@MainActor public protocol DiagnosticClipboard {
    func write(_ text: String) -> Bool
}

public enum DiagnosticStatus: Equatable, Sendable {
    case idle
    case capturing
    case copied(Int)
    case failed(String)

    public var label: String {
        switch self {
        case .idle: "Ready"
        case .capturing: "Capturing…"
        case .copied(let count): "Copied — \(count) fields"
        case .failed(let reason): "Failed — \(reason)"
        }
    }
}

@MainActor private final class DiagnosticCompletion {
    private var continuation: CheckedContinuation<FieldDiagnosticDTO, Error>?
    private var finished = false

    func install(_ continuation: CheckedContinuation<FieldDiagnosticDTO, Error>) {
        self.continuation = continuation
    }

    func resolve(_ result: Result<FieldDiagnosticDTO, Error>) {
        guard !finished else { return }
        finished = true
        continuation?.resume(with: result)
        continuation = nil
    }
}

@MainActor
public final class AppStore: ObservableObject {
    @Published public private(set) var connection: BackendConnection = .disconnected
    @Published public private(set) var runs: [RunDTO] = []
    @Published public private(set) var applications: [ApplicationDTO] = []
    @Published public private(set) var attention: [ApplicationDTO] = []
    @Published public private(set) var selectedApplication: ApplicationDTO?
    @Published public private(set) var report: [ReportEntryDTO] = []
    @Published public private(set) var reportPendingReviewCount: Int?
    @Published public private(set) var narratives: [ReportEntryDTO] = []
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var isLoading = false
    @Published public private(set) var recoveringTaskId: String?
    @Published public private(set) var resumingTaskId: String?
    @Published public private(set) var startingApplicationURL = false
    @Published public private(set) var foregroundingTaskId: String?
    @Published public private(set) var diagnosticStatus: DiagnosticStatus = .idle
    /// One explicit field attestation at a time: while it is in flight every
    /// Mark Resolved control is disabled, so a repeated click cannot land on
    /// the next card after the report re-sorts.
    @Published public private(set) var resolvingEntryId: String? = nil
    @Published public private(set) var lastCapturedDiagnostic: String?
    @Published public var selectedRunId: String?
    @Published public var selectedTaskId: String?
    @Published public var showsAttention = false

    private let client: (any ControlPlaneClient)?
    private let diagnosticTimeout: Duration
    private let diagnosticSuccessDuration: Duration
    private var diagnosticRequestID: UUID?

    public init(client: (any ControlPlaneClient)?, diagnosticTimeout: Duration = .seconds(15),
                diagnosticSuccessDuration: Duration = .seconds(4)) {
        self.client = client
        self.diagnosticTimeout = diagnosticTimeout
        self.diagnosticSuccessDuration = diagnosticSuccessDuration
        if client == nil {
            errorMessage = "Backend launch is not configured. Install Job Application Agent.app or set JOBAGENT_BACKEND_ROOT and JOBAGENT_DB_PATH for development."
        }
    }

    public var visibleApplications: [ApplicationDTO] {
        if showsAttention { return attention }
        guard let selectedRunId else { return applications }
        return applications.filter { $0.runId == selectedRunId }
    }

    public var canReconnect: Bool {
        client != nil && connection != .connected && connection != .starting
    }

    public var visibleNeedsAttentionCount: Int {
        report.filter { $0.kind == "field" && $0.reportGroup == "needs_attention" }.count
    }

    public var visibleNeedsReviewCount: Int {
        report.filter { $0.kind == "field" && $0.reportGroup == "needs_review" }.count
    }

    public func reconnect() async {
        guard let client else { return }
        connection = .starting
        do {
            try await client.restart()
            connection = await client.connectionState()
            errorMessage = nil
            await refresh()
        } catch {
            await record(error)
        }
    }

    public func refresh() async {
        guard let client else { return }
        connection = await client.connectionState()
        guard connection == .connected else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            runs = try await client.send(.listRuns, as: [RunDTO].self)
            applications = try await client.send(.listApplications(nil), as: [ApplicationDTO].self)
            attention = try await client.send(.listAttentionRequired(nil), as: [ApplicationDTO].self)
            if let selectedRunId, !runs.contains(where: { $0.id == selectedRunId }) {
                self.selectedRunId = nil
            }
            if let selectedTaskId, !applications.contains(where: { $0.id == selectedTaskId }) {
                self.selectedTaskId = nil
                selectedApplication = nil
                report = []
                narratives = []
            }
            if let selectedTaskId { try await loadDetail(selectedTaskId, client: client) }
            errorMessage = nil
        } catch {
            await record(error)
        }
    }

    public func selectApplication(_ taskId: String?) async {
        selectedTaskId = taskId
        selectedApplication = nil
        report = []
        reportPendingReviewCount = nil
        narratives = []
        guard let taskId, let client else { return }
        do {
            try await loadDetail(taskId, client: client)
            errorMessage = nil
        } catch {
            await record(error)
        }
    }

    public func startApplicationURL(_ url: String) async {
        guard let client, !startingApplicationURL else { return }
        startingApplicationURL = true
        defer { startingApplicationURL = false }
        do {
            let application: ApplicationDTO = try await client.send(
                .startApplicationURL(url), as: ApplicationDTO.self)
            selectedRunId = application.runId
            showsAttention = false
            selectedTaskId = application.id
            selectedApplication = application
            if !applications.contains(where: { $0.id == application.id }) {
                applications.insert(application, at: 0)
            }
            errorMessage = nil
        } catch { await record(error) }
    }

    public func resume(_ taskId: String) async {
        guard let client, resumingTaskId == nil else { return }
        resumingTaskId = taskId
        defer { resumingTaskId = nil }
        do {
            let application: ApplicationDTO = try await client.send(
                .resumeApplication(taskId), as: ApplicationDTO.self)
            // The idle worker now owns the browser pass. Reflect the durable
            // QUEUED transition immediately; the regular refresh reads its
            // fresh, reconciled report after that pass yields.
            if selectedTaskId == taskId { selectedApplication = application }
            applications = applications.map { $0.id == taskId ? application : $0 }
            attention = attention.map { $0.id == taskId ? application : $0 }
            errorMessage = nil
        } catch { await record(error) }
    }

    public func openApplication(_ taskId: String) async {
        guard let client else { return }
        do {
            let _: ApplicationDTO = try await client.send(.openApplication(taskId), as: ApplicationDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func recoverApplication(_ taskId: String) async {
        guard let client, recoveringTaskId == nil else { return }
        recoveringTaskId = taskId
        defer { recoveringTaskId = nil }
        do {
            let _: ApplicationDTO = try await client.send(.recoverApplication(taskId), as: ApplicationDTO.self)
            await refresh()
        } catch {
            await refresh()
            await record(error)
        }
    }

    public func archiveApplication(_ taskId: String) async {
        guard let client, !isLoading else { return }
        do {
            let _: ArchiveApplicationDTO = try await client.send(
                .archiveApplication(taskId), as: ArchiveApplicationDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func bringWindowToFront(_ taskId: String) async {
        guard let client, foregroundingTaskId == nil else { return }
        foregroundingTaskId = taskId
        defer { foregroundingTaskId = nil }
        do {
            let _: ForegroundDTO = try await client.send(.bringWindowToFront(taskId), as: ForegroundDTO.self)
            errorMessage = nil
        } catch { await record(error) }
    }

    public func copyFieldDiagnostic(_ taskId: String, clipboard: any DiagnosticClipboard) async {
        guard diagnosticStatus != .capturing else { return }
        let requestID = UUID()
        diagnosticRequestID = requestID
        diagnosticStatus = .capturing
        guard let client else {
            diagnosticStatus = .failed("backend unavailable")
            return
        }
        do {
            let timeout = diagnosticTimeout
            let result: FieldDiagnosticDTO = try await withCheckedThrowingContinuation { continuation in
                let completion = DiagnosticCompletion()
                completion.install(continuation)
                Task {
                    do {
                        let result: FieldDiagnosticDTO = try await client.send(
                            .diagnoseCurrentFields(taskId), as: FieldDiagnosticDTO.self)
                        completion.resolve(.success(result))
                    } catch { completion.resolve(.failure(error)) }
                }
                Task {
                    try? await Task.sleep(for: timeout)
                    completion.resolve(.failure(ControlClientError.diagnosticTimeout))
                }
            }
            guard diagnosticRequestID == requestID else { return }
            guard result.taskId == taskId,
                  let capturedAt = result.capturedAt, !capturedAt.isEmpty,
                  let generation = result.diagnosticGeneration, !generation.isEmpty else {
                throw ControlClientError.protocolMismatch
            }
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(result)
            guard let text = String(data: data, encoding: .utf8) else {
                throw ControlClientError.protocolMismatch
            }
            lastCapturedDiagnostic = text
            guard clipboard.write(text) else {
                diagnosticStatus = .failed("clipboard error")
                return
            }
            diagnosticStatus = .copied(result.fieldCount)
            errorMessage = nil
            let successDuration = diagnosticSuccessDuration
            Task { [weak self] in
                try? await Task.sleep(for: successDuration)
                guard let self, self.diagnosticRequestID == requestID,
                      case .copied = self.diagnosticStatus else { return }
                self.diagnosticStatus = .idle
            }
        } catch {
            guard diagnosticRequestID == requestID else { return }
            if let controlError = error as? ControlClientError {
                switch controlError {
                case .diagnosticTimeout, .requestTimeout: diagnosticStatus = .failed("timed out")
                case .unavailable, .transport: diagnosticStatus = .failed("backend unavailable")
                case .backend(let detail):
                    diagnosticStatus = .failed(detail.code == "WINDOW_UNAVAILABLE"
                                               ? "managed page unavailable" : detail.message)
                case .protocolMismatch: diagnosticStatus = .failed("invalid fresh response")
                }
            } else {
                diagnosticStatus = .failed(error.localizedDescription)
            }
        }
    }

    public func reviewEntry(_ entryId: String) async {
        guard let client else { return }
        do {
            let _: ReportEntryDTO = try await client.send(.reviewReportEntry(entryId), as: ReportEntryDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func resolveField(_ entryId: String) async {
        guard let client, resolvingEntryId == nil else { return }
        resolvingEntryId = entryId
        defer { resolvingEntryId = nil }
        do {
            let _: ReportEntryDTO = try await client.send(.resolveField(entryId), as: ReportEntryDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    /// Reopens one manually resolved field. Shares the single in-flight
    /// guard with Mark Resolved; never acts on the employer page.
    public func undoFieldResolution(_ entryId: String) async {
        guard let client, resolvingEntryId == nil else { return }
        resolvingEntryId = entryId
        defer { resolvingEntryId = nil }
        do {
            let _: ReportEntryDTO = try await client.send(.undoFieldResolution(entryId), as: ReportEntryDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func replaceNarrative(_ entryId: String, with text: String) async {
        guard let client else { return }
        do {
            let _: ReportEntryDTO = try await client.send(
                .replaceNarrative(entryID: entryId, text: text), as: ReportEntryDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func markFinalReviewChecked(_ taskId: String) async {
        guard let client else { return }
        do {
            let _: ApplicationDTO = try await client.send(.markFinalReviewChecked(taskId), as: ApplicationDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func recordSubmission(_ taskId: String) async {
        guard let client else { return }
        do {
            let _: ApplicationDTO = try await client.send(.recordSubmission(taskId), as: ApplicationDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    private func loadDetail(_ taskId: String, client: any ControlPlaneClient) async throws {
        let detail: ApplicationDTO = try await client.send(.getApplication(taskId), as: ApplicationDTO.self)
        let fullReport: ReportDTO = try await client.send(.getApplicationReport(taskId), as: ReportDTO.self)
        let narrativeReport: ReportDTO = try await client.send(.getNarrativeEntries(taskId), as: ReportDTO.self)
        // Selection can change while the three queries are in flight.
        guard selectedTaskId == taskId else { return }
        selectedApplication = detail
        report = fullReport.entries
        reportPendingReviewCount = fullReport.pendingReviewCount
        narratives = narrativeReport.entries
    }

    private func record(_ error: Error) async {
        errorMessage = error.localizedDescription
        if let client { connection = await client.connectionState() }
    }
}
