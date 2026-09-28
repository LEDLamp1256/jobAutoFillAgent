import Combine
import Foundation

@MainActor
public final class AppStore: ObservableObject {
    @Published public private(set) var connection: BackendConnection = .disconnected
    @Published public private(set) var runs: [RunDTO] = []
    @Published public private(set) var applications: [ApplicationDTO] = []
    @Published public private(set) var attention: [ApplicationDTO] = []
    @Published public private(set) var selectedApplication: ApplicationDTO?
    @Published public private(set) var report: [ReportEntryDTO] = []
    @Published public private(set) var narratives: [ReportEntryDTO] = []
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var isLoading = false
    @Published public var selectedRunId: String?
    @Published public var selectedTaskId: String?
    @Published public var showsAttention = false

    private let client: (any ControlPlaneClient)?

    public init(client: (any ControlPlaneClient)?) {
        self.client = client
        if client == nil {
            errorMessage = "Backend launch is not configured. Set JOBAGENT_BACKEND_ROOT and JOBAGENT_DB_PATH."
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
            if selectedRunId == nil { selectedRunId = runs.first?.id }
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
        narratives = []
        guard let taskId, let client else { return }
        do {
            try await loadDetail(taskId, client: client)
            errorMessage = nil
        } catch {
            await record(error)
        }
    }

    public func resume(_ taskId: String) async {
        guard let client else { return }
        do {
            let _: ApplicationDTO = try await client.send(.resumeApplication(taskId), as: ApplicationDTO.self)
            await refresh()
        } catch { await record(error) }
    }

    public func bringWindowToFront(_ taskId: String) async {
        guard let client else { return }
        do {
            let _: ForegroundDTO = try await client.send(.bringWindowToFront(taskId), as: ForegroundDTO.self)
            errorMessage = nil
        } catch { await record(error) }
    }

    public func reviewEntry(_ entryId: String) async {
        guard let client else { return }
        do {
            let _: ReportEntryDTO = try await client.send(.reviewReportEntry(entryId), as: ReportEntryDTO.self)
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

    private func loadDetail(_ taskId: String, client: any ControlPlaneClient) async throws {
        let detail: ApplicationDTO = try await client.send(.getApplication(taskId), as: ApplicationDTO.self)
        let fullReport: ReportDTO = try await client.send(.getApplicationReport(taskId), as: ReportDTO.self)
        let narrativeReport: ReportDTO = try await client.send(.getNarrativeEntries(taskId), as: ReportDTO.self)
        // Selection can change while the three queries are in flight.
        guard selectedTaskId == taskId else { return }
        selectedApplication = detail
        report = fullReport.entries
        narratives = narrativeReport.entries
    }

    private func record(_ error: Error) async {
        errorMessage = error.localizedDescription
        if let client { connection = await client.connectionState() }
    }
}
