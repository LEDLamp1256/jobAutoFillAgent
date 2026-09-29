import Foundation
import XCTest
@testable import JobAgentControlCore

private func applicationJSON(status: String = "human_paused", resume: Bool = true,
                             ready: Bool = false) -> Data {
    let object: [String: Any] = [
        "task_id": "task-a", "run_id": "run-a", "listing_id": "listing-a",
        "company": "Acme", "title": "Engineer", "status": status,
        "ownership": resume ? "human_owned" : "automation_owned",
        "current_page_or_step": NSNull(), "attention_category": resume ? "human_paused" : NSNull(),
        "blocker": resume ? "needs_answer" : NSNull(), "failure_reason": NSNull(),
        "window_associated": true, "window_available": false,
        "resume_available": resume, "ready_for_review": ready,
        "final_review_available": ready, "final_review_checked": false,
        "pending_review_count": 0, "pending_narrative_count": 0,
        "created_at": "2026-09-28T00:00:00+00:00", "updated_at": "2026-09-28T00:00:00+00:00",
    ]
    return try! JSONSerialization.data(withJSONObject: object)
}

private let runJSON = Data("""
{"run_id":"run-a","status":"running","created_at":"2026-09-28T00:00:00+00:00",\
"completed_at":null,"requested_sources":["board"],"requested_job_limit":3,\
"discovered_count":1,"queued_count":1}
""".utf8)

private let reportEntryJSON = Data("""
{"entry_id":"entry-a","task_id":"task-a","page_or_step":null,"visible_label":"Why us?",\
"semantic_key":null,"kind":"narrative","provenance":"ai_draft_review","action":"drafted",\
"verification":"unknown","review_state":"pending","reason":null,\
"created_at":"2026-09-28T00:00:00+00:00","narrative_text":"Draft"}
""".utf8)

private func narrative(id: String, provenance: String, action: String, reviewState: String,
                       createdAt: String, text: String) -> ReportEntryDTO {
    ReportEntryDTO(entryId: id, taskId: "task-a", pageOrStep: "page 1", visibleLabel: "Why us?",
                   semanticKey: "motivation", kind: "narrative", provenance: provenance,
                   action: action, verification: "unknown", reviewState: reviewState,
                   reason: nil, createdAt: createdAt, narrativeText: text)
}

private actor FakeClient: ControlPlaneClient {
    private(set) var operations: [ControlOperation] = []
    private(set) var restartCount = 0
    var failure: ControlClientError?
    private var connected = false

    func restart() { connected = true; restartCount += 1 }
    func connectionState() -> BackendConnection { connected ? .connected : .disconnected }

    func send<T: Decodable & Sendable>(_ operation: ControlOperation, as type: T.Type) throws -> T {
        operations.append(operation)
        if let failure { throw failure }
        let data: Data
        switch operation {
        case .listRuns: data = Data("[\(String(decoding: runJSON, as: UTF8.self))]".utf8)
        case .listApplications, .listAttentionRequired:
            data = Data("[\(String(decoding: applicationJSON(), as: UTF8.self))]".utf8)
        case .getApplication: data = applicationJSON()
        case .getApplicationReport, .getNarrativeEntries:
            data = Data("{\"task_id\":\"task-a\",\"entries\":[]}".utf8)
        case .resumeApplication: data = applicationJSON(status: "queued", resume: false)
        case .bringWindowToFront:
            data = Data("{\"task_id\":\"task-a\",\"foregrounded\":true}".utf8)
        case .reviewReportEntry, .replaceNarrative: data = reportEntryJSON
        case .markFinalReviewChecked: data = applicationJSON(status: "ready_for_review", resume: false, ready: true)
        case .getRun: data = runJSON
        }
        return try JSONDecoder.controlPlane.decode(T.self, from: data)
    }
}

final class ControlPlaneTests: XCTestCase {
    func testBundledRuntimeUsesStableSupportPathsOutsideApp() throws {
        let home = URL(fileURLWithPath: "/Users/local-test", isDirectory: true)
        let resources = URL(fileURLWithPath: "/tmp/Job Application Agent.app/Contents/Resources", isDirectory: true)
        let configuration = try XCTUnwrap(BackendConfiguration.fromEnvironment(
            [:], bundleResources: resources, homeDirectory: home, isBundledApplication: true))
        let support = "/Users/local-test/Library/Application Support/Job Application Agent"
        XCTAssertEqual(configuration.repositoryRoot.path, resources.appendingPathComponent("Backend").path)
        XCTAssertEqual(configuration.pythonExecutable, support + "/runtime/python/bin/python")
        XCTAssertEqual(configuration.nodeExecutable, support + "/runtime/bin/node")
        XCTAssertEqual(configuration.mcpCLIPath,
                       support + "/runtime/playwright-mcp/node_modules/@playwright/mcp/cli.js")
        XCTAssertEqual(configuration.databasePath, support + "/applications.sqlite3")
        XCTAssertEqual(configuration.configurationPath, support + "/config.json")
        XCTAssertFalse(configuration.databasePath.hasPrefix(resources.path))
        XCTAssertFalse(try XCTUnwrap(configuration.configurationPath).hasPrefix(resources.path))
        XCTAssertNil(BackendConfiguration.fromEnvironment(
            [:], bundleResources: resources, homeDirectory: home, isBundledApplication: false))
    }

    func testDevelopmentEnvironmentKeepsExistingBackendContract() throws {
        let configuration = try XCTUnwrap(BackendConfiguration.fromEnvironment(
            ["JOBAGENT_BACKEND_ROOT": "/tmp/source", "JOBAGENT_DB_PATH": "/tmp/dev.sqlite3",
             "JOBAGENT_PYTHON": "/tmp/python"], bundleResources: nil,
            isBundledApplication: false))
        XCTAssertEqual(configuration.repositoryRoot.path, "/tmp/source")
        XCTAssertEqual(configuration.databasePath, "/tmp/dev.sqlite3")
        XCTAssertEqual(configuration.pythonExecutable, "/tmp/python")
        XCTAssertNil(configuration.configurationPath)
    }

    func testMissingPythonAndUnavailableSupportGiveActionableErrors() async throws {
        let missing = ControlPlaneProcess(configuration: BackendConfiguration(
            pythonExecutable: "/nonexistent/jobagent-python", repositoryRoot: URL(fileURLWithPath: "/tmp"),
            databasePath: "/tmp/unused.sqlite3"))
        do {
            try await missing.restart()
            XCTFail("Missing Python must fail before launching a child")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("Python runtime is missing"))
        }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let backend = directory.appendingPathComponent("jobagent")
        try FileManager.default.createDirectory(at: backend, withIntermediateDirectories: true)
        try Data().write(to: backend.appendingPathComponent("control_plane_stdio.py"))
        let blocked = directory.appendingPathComponent("support-file")
        try Data().write(to: blocked)
        let client = ControlPlaneProcess(configuration: BackendConfiguration(
            pythonExecutable: "/usr/bin/python3", repositoryRoot: directory,
            databasePath: blocked.appendingPathComponent("applications.sqlite3").path,
            supportDirectory: blocked))
        do {
            try await client.restart()
            XCTFail("An unusable support directory must fail visibly")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("Application Support"))
        }
    }

    func testMissingMCPAndDatabaseOpenFailureAreVisible() async throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let python = ProcessInfo.processInfo.environment["JOBAGENT_TEST_PYTHON"] ?? "/usr/bin/python3"
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let missingMCP = ControlPlaneProcess(configuration: BackendConfiguration(
            pythonExecutable: python, repositoryRoot: root,
            databasePath: directory.appendingPathComponent("unused.sqlite3").path,
            mcpCLIPath: directory.appendingPathComponent("missing-cli.js").path))
        do {
            try await missingMCP.restart()
            XCTFail("Missing MCP must fail before launching Python")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("Playwright MCP runtime is missing"))
        }
        let invalidDatabase = ControlPlaneProcess(configuration: BackendConfiguration(
            pythonExecutable: python, repositoryRoot: root, databasePath: directory.path))
        do {
            try await invalidDatabase.restart()
            XCTFail("SQLite cannot open a directory as a database")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("database"))
        }
        await invalidDatabase.shutdown()
    }

    func testNarrativeReplacementIsCurrentAndOriginalRemainsHistory() {
        let draft = narrative(id: "draft", provenance: "ai_draft_review", action: "drafted",
                              reviewState: "approved", createdAt: "2026-09-28T00:00:00Z", text: "AI text")
        let replacement = narrative(id: "replacement", provenance: "human_provided", action: "replaced",
                                    reviewState: "approved", createdAt: "2026-09-28T00:01:00Z", text: "Owner text")
        let items = NarrativePresentation.items(from: [draft, replacement])
        XCTAssertEqual(items.count, 1)
        XCTAssertEqual(items[0].current.id, "replacement")
        XCTAssertEqual(items[0].current.narrativeText, "Owner text")
        XCTAssertEqual(items[0].history.map(\.id), ["draft"])
        XCTAssertEqual(items[0].history[0].narrativeText, "AI text")

        let ambiguous = NarrativePresentation.items(from: [draft, draftWithDifferentID(draft), replacement])
        XCTAssertEqual(ambiguous.count, 3)
        XCTAssertTrue(ambiguous.allSatisfy { $0.history.isEmpty })
    }

    private func draftWithDifferentID(_ draft: ReportEntryDTO) -> ReportEntryDTO {
        narrative(id: "second-draft", provenance: draft.provenance, action: draft.action,
                  reviewState: draft.reviewState, createdAt: "2026-09-28T00:00:30Z", text: "Other AI text")
    }

    func testWireDecodingSuccessErrorNullsAndNarrative() throws {
        let success = Data("{\"id\":\"1\",\"ok\":true,\"result\":\(String(decoding: applicationJSON(), as: UTF8.self))}".utf8)
        let envelope = try JSONDecoder.controlPlane.decode(ResponseEnvelope<ApplicationDTO>.self, from: success)
        XCTAssertEqual(envelope.result?.taskId, "task-a")
        XCTAssertNil(envelope.result?.currentPageOrStep)
        XCTAssertEqual(envelope.result?.blocker, "needs_answer")
        let failure = Data("{\"id\":\"2\",\"ok\":false,\"error\":{\"code\":\"WINDOW_UNAVAILABLE\",\"message\":\"managed window is unavailable\"}}".utf8)
        let error = try JSONDecoder.controlPlane.decode(ResponseEnvelope<ApplicationDTO>.self, from: failure)
        XCTAssertEqual(error.error?.code, "WINDOW_UNAVAILABLE")
        let report = try JSONDecoder.controlPlane.decode(ReportEntryDTO.self, from: reportEntryJSON)
        XCTAssertEqual(report.provenance, "ai_draft_review")
        XCTAssertEqual(report.narrativeText, "Draft")
        XCTAssertNil(report.pageOrStep)
    }

    func testRequestEncodingHasFixedMethodsAndNoAuthorizationFields() throws {
        let resume = try XCTUnwrap(String(data: ControlOperation.resumeApplication("task-a").encodedLine(id: "1"), encoding: .utf8))
        XCTAssertTrue(resume.hasSuffix("\n"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(resume.utf8)) as? [String: Any])
        XCTAssertEqual(object["method"] as? String, "resume_application")
        XCTAssertEqual(object["params"] as? [String: String], ["task_id": "task-a"])
        XCTAssertNil(object["actor"])
        XCTAssertNil(object["action"])
        let foreground = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.bringWindowToFront("task-a").encodedLine(id: "2")) as? [String: Any])
        XCTAssertEqual(foreground["method"] as? String, "bring_window_to_front")
        XCTAssertNotEqual(foreground["method"] as? String, object["method"] as? String)
    }

    @MainActor
    func testSelectionDoesNotForegroundButExplicitCommandDoes() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        var operations = await fake.operations
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        await store.bringWindowToFront("task-a")
        operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .bringWindowToFront("task-a") }.count, 1)
    }

    @MainActor
    func testResumeRefreshErrorAndNarrativeSaveAreExplicit() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        let beforeEdit = await fake.operations
        XCTAssertFalse(beforeEdit.contains(.replaceNarrative(entryID: "entry-a", text: "Human")))
        await store.replaceNarrative("entry-a", with: "Human")
        let afterEdit = await fake.operations
        XCTAssertTrue(afterEdit.contains(.replaceNarrative(entryID: "entry-a", text: "Human")))
        await store.resume("task-a")
        let operations = await fake.operations
        XCTAssertTrue(operations.contains(.resumeApplication("task-a")))
        XCTAssertGreaterThan(operations.filter { $0 == .listRuns }.count, 1)
        await fake.setFailure(.backend(BackendErrorDTO(code: "NOT_RESUMABLE", message: "not paused")))
        await store.resume("task-a")
        XCTAssertTrue(store.errorMessage?.contains("NOT_RESUMABLE") == true)
        XCTAssertFalse(operations.contains(.markFinalReviewChecked("task-a")))
        await fake.setFailure(nil)
        await store.markFinalReviewChecked("task-a")
        let afterReview = await fake.operations
        XCTAssertTrue(afterReview.contains(.markFinalReviewChecked("task-a")))
    }

    @MainActor
    func testRefreshOnlyQueriesAndReconnectOnlyRestartsBackend() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        XCTAssertTrue(store.canReconnect)
        await store.reconnect()
        XCTAssertFalse(store.canReconnect)
        let beforeRefresh = await fake.restartCount
        let beforeOperations = await fake.operations.count
        await store.refresh()
        let afterRefresh = await fake.restartCount
        XCTAssertEqual(afterRefresh, beforeRefresh)
        let refreshOperations = Array((await fake.operations).dropFirst(beforeOperations))
        XCTAssertEqual(refreshOperations, [.listRuns, .listApplications(nil), .listAttentionRequired(nil)])
        await store.reconnect()
        let afterReconnect = await fake.restartCount
        XCTAssertEqual(afterReconnect, beforeRefresh + 1)
        let operations = await fake.operations
        XCTAssertFalse(operations.contains(.resumeApplication("task-a")))
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        XCTAssertFalse(operations.contains(.markFinalReviewChecked("task-a")))
    }

    func testRealPythonChildSmokeAndControlledDisconnect() async throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let database = directory.appendingPathComponent("smoke.sqlite3")
        let python = ProcessInfo.processInfo.environment["JOBAGENT_TEST_PYTHON"] ?? "/usr/bin/python3"
        let client = ControlPlaneProcess(configuration: BackendConfiguration(
            pythonExecutable: python, repositoryRoot: root, databasePath: database.path))
        try await client.restart()
        let runs: [RunDTO] = try await client.send(.listRuns, as: [RunDTO].self)
        XCTAssertEqual(runs.count, 0)
        let connected = await client.connectionState()
        XCTAssertEqual(connected, .connected)
        await client.shutdown()
        let disconnected = await client.connectionState()
        XCTAssertEqual(disconnected, .disconnected)
        try await client.restart()
        let again: [RunDTO] = try await client.send(.listRuns, as: [RunDTO].self)
        XCTAssertEqual(again.count, 0)
        await client.shutdown()
    }

    func testUnexpectedChildExitProducesStartupError() async throws {
        let configuration = BackendConfiguration(
            pythonExecutable: "/usr/bin/false",
            repositoryRoot: URL(fileURLWithPath: #filePath).deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent(),
            databasePath: "/private/tmp/unused-jobagent-test.sqlite3")
        let client = ControlPlaneProcess(configuration: configuration)
        do {
            try await client.restart()
            XCTFail("A child that exits immediately must fail the startup handshake")
        } catch {
            XCTAssertTrue(error.localizedDescription.contains("database"))
        }
        await client.shutdown()
    }
}

private extension FakeClient {
    func setFailure(_ error: ControlClientError?) { failure = error }
}
