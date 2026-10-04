import Foundation
import XCTest
@testable import JobAgentControlCore

private func applicationJSON(status: String = "human_paused", resume: Bool = true,
                             ready: Bool = false, phase: String? = nil,
                             pending: Int = 0, checked: Bool = false,
                             recordSubmission: Bool = false,
                             title: String = "Engineer", company: String = "Acme") -> Data {
    let object: [String: Any] = [
        "task_id": "task-a", "run_id": "run-a", "listing_id": "listing-a",
        "company": company, "title": title, "status": status,
        "ownership": resume ? "human_owned" : "automation_owned",
        "current_page_or_step": NSNull(), "attention_category": resume ? "human_paused" : NSNull(),
        "blocker": resume ? "needs_answer" : NSNull(), "failure_reason": NSNull(),
        "window_associated": true, "window_available": false,
        "resume_available": resume, "ready_for_review": ready,
        "review_phase": phase.map { $0 as Any } ?? NSNull(),
        "final_review_available": ready && pending == 0 && !checked,
        "final_review_checked": checked,
        "record_submission_available": recordSubmission,
        "pending_review_count": pending, "pending_narrative_count": 0,
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
                   reason: nil, createdAt: createdAt, narrativeText: text,
                   requiredness: nil, category: nil)
}

private actor FakeClient: ControlPlaneClient {
    private(set) var operations: [ControlOperation] = []
    private(set) var restartCount = 0
    var failure: ControlClientError?
    private var diagnosticDelay: Duration?
    private var resolveDelay: Duration?
    private var resumeDelay: Duration?
    private var startDelay: Duration?
    private var foregroundDelay: Duration?
    private var diagnosticSequence = 0
    private var connected = false
    private var archived = false
    private var applicationData = applicationJSON()
    private var reportData = Data("{\"task_id\":\"task-a\",\"entries\":[]}".utf8)

    func setApplicationData(_ data: Data) { applicationData = data }
    func setReportData(_ data: Data) { reportData = data }
    func setDiagnosticDelay(_ delay: Duration?) { diagnosticDelay = delay }
    func setResolveDelay(_ delay: Duration?) { resolveDelay = delay }
    func setResumeDelay(_ delay: Duration?) { resumeDelay = delay }
    func setStartDelay(_ delay: Duration?) { startDelay = delay }
    func setForegroundDelay(_ delay: Duration?) { foregroundDelay = delay }

    func restart() { connected = true; restartCount += 1 }
    func connectionState() -> BackendConnection { connected ? .connected : .disconnected }

    func send<T: Decodable & Sendable>(_ operation: ControlOperation, as type: T.Type) async throws -> T {
        operations.append(operation)
        if case .diagnoseCurrentFields = operation, let diagnosticDelay {
            try await Task.sleep(for: diagnosticDelay)
        }
        if case .resolveField = operation, let resolveDelay {
            try await Task.sleep(for: resolveDelay)
        }
        if case .undoFieldResolution = operation, let resolveDelay {
            try await Task.sleep(for: resolveDelay)
        }
        if case .resumeApplication = operation, let resumeDelay {
            try await Task.sleep(for: resumeDelay)
        }
        if case .startApplicationURL = operation, let startDelay {
            try await Task.sleep(for: startDelay)
        }
        if case .bringWindowToFront = operation, let foregroundDelay {
            try await Task.sleep(for: foregroundDelay)
        }
        if let failure { throw failure }
        let data: Data
        switch operation {
        case .startApplicationURL:
            data = applicationData
        case .listRuns: data = Data("[\(String(decoding: runJSON, as: UTF8.self))]".utf8)
        case .listApplications:
            data = archived ? Data("[]".utf8) :
                Data("[\(String(decoding: applicationData, as: UTF8.self))]".utf8)
        case .listAttentionRequired:
            data = archived || String(decoding: applicationData, as: UTF8.self).contains("\"status\":\"submitted_by_human\"")
                ? Data("[]".utf8)
                : Data("[\(String(decoding: applicationData, as: UTF8.self))]".utf8)
        case .getApplication: data = applicationData
        case .getApplicationReport:
            data = reportData
        case .getNarrativeEntries:
            data = Data("{\"task_id\":\"task-a\",\"entries\":[]}".utf8)
        case .diagnoseCurrentFields:
            diagnosticSequence += 1
            data = Data("""
            {"task_id":"task-a","captured_at":"2026-10-01T00:00:00Z",
             "diagnostic_generation":"generation-\(diagnosticSequence)","field_count":1,"truncated":false,
             "discovery_summary":{"normalized_field_count":1,"accessibility_question_count":0,
              "dom_recovered_field_count":1,"raw_actionable_count":2,"ignored_actionable_count":1,
              "ignored_reasons":{"navigation_or_submit":1},"navigation_count":1,
              "section_action_count":0,"truncated":false},"fields":[
            {"label":"State","question_identity":"abc","section":null,"record_context":null,
             "raw_role":"button","interaction_kind":"choice","requiredness":"required",
             "current_value_present":true,"required_evidence":"group_required",
             "current_answer":"[redacted]","answer_evidence":"dom_value",
             "placeholder":false,"satisfied":true,"automation_capability":"unsupported",
             "resolver_capability":"not_evaluated","blocking":false,"report_group":"completed",
             "manual_resolution":"human_attested","discovery_source":"dom_fallback","state_conflict":false}]}
            """.utf8)
        case .resumeApplication: data = applicationJSON(status: "queued", resume: false)
        case .openApplication: data = applicationJSON(status: "queued", resume: false)
        case .recoverApplication: data = applicationJSON(status: "human_paused", resume: true)
        case .archiveApplication:
            archived = true
            data = Data("{\"task_id\":\"task-a\",\"archived\":true,\"removed_from_queue\":false}".utf8)
        case .bringWindowToFront:
            data = Data("{\"task_id\":\"task-a\",\"foregrounded\":true}".utf8)
        case .reviewReportEntry, .resolveField, .undoFieldResolution, .replaceNarrative:
            data = reportEntryJSON
        case .markFinalReviewChecked: data = applicationJSON(status: "ready_for_review", resume: false, ready: true)
        case .recordSubmission:
            applicationData = applicationJSON(status: "submitted_by_human", resume: false,
                                              phase: "submitted", checked: true)
            data = applicationData
        case .getRun: data = runJSON
        }
        return try JSONDecoder.controlPlane.decode(T.self, from: data)
    }
}

@MainActor private final class FakeDiagnosticClipboard: DiagnosticClipboard {
    var writes: [String] = []
    var succeeds = true
    func write(_ text: String) -> Bool {
        writes.append(text)
        return succeeds
    }
}

final class ControlPlaneTests: XCTestCase {
    func testRunHistoryAndUndoDialogCopyDoNotMisstateQueueOrUseLongTitle() throws {
        let run = try JSONDecoder.controlPlane.decode(RunDTO.self, from: runJSON)
        XCTAssertEqual(run.status, "running")
        XCTAssertEqual(run.taskHistoryLabel, "1 application task created")
        XCTAssertFalse(run.taskHistoryLabel.contains("queued"))
        XCTAssertEqual(run.historyLabel(applications: []), "board")
        let direct = RunDTO(runId: "direct", status: "running", createdAt: run.createdAt,
                            completedAt: nil, requestedSources: ["direct_url"],
                            requestedJobLimit: 1, discoveredCount: 1, queuedCount: 1)
        XCTAssertEqual(direct.historyLabel(applications: []), "Direct application")
        let linked = try JSONDecoder.controlPlane.decode(ApplicationDTO.self,
            from: applicationJSON(title: "Engineer", company: "Acme"))
        XCTAssertEqual(run.historyLabel(applications: [linked]), "Engineer · Acme")
        let longLabel = "Have you previously worked for or are you currently working for Workday?"
        let entry = ReportEntryDTO(entryId: "field-a", taskId: "task-a", pageOrStep: "page 1",
            visibleLabel: longLabel, semanticKey: nil, kind: "field", provenance: "human_provided",
            action: "human_resolved", verification: "not_attempted", reviewState: "not_required",
            reason: "human_attested", createdAt: "2026-09-28T00:00:00+00:00",
            narrativeText: nil, requiredness: "required", category: "application")
        XCTAssertEqual(entry.undoDialogTitle, "Undo Mark Resolved")
        XCTAssertFalse(entry.undoDialogTitle.contains(longLabel))
        XCTAssertTrue(entry.undoDialogMessage.contains(longLabel))
    }

    @MainActor
    func testArchiveApplicationIsExplicitLocalCommandAndRemovesVisibleRow() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        XCTAssertEqual(store.applications.count, 1)
        await store.archiveApplication("task-a")
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .archiveApplication("task-a") }.count, 1)
        XCTAssertEqual(store.applications.count, 0)
        XCTAssertNil(store.selectedApplication)
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        XCTAssertFalse(operations.contains(.recordSubmission("task-a")))
    }

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

    func testReportRequirednessLabelsAndObservedApplicationDisplayMetadata() throws {
        for (wire, expected) in [("required", "Required"), ("optional", "Optional"),
                                 ("unknown", "Requiredness Unknown")] {
            var object = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON) as? [String: Any])
            object["requiredness"] = wire
            let entry = try JSONDecoder.controlPlane.decode(
                ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
            XCTAssertEqual(entry.requirednessLabel, expected)
        }
        let legacy = try JSONDecoder.controlPlane.decode(ReportEntryDTO.self, from: reportEntryJSON)
        XCTAssertNil(legacy.requirednessLabel)
        let application = try JSONDecoder.controlPlane.decode(ApplicationDTO.self, from: applicationJSON(
            title: "Software Development Engineer - US Federal", company: "Example Company"))
        XCTAssertEqual(application.title, "Software Development Engineer - US Federal")
        XCTAssertEqual(application.company, "Example Company")
        let titleOnly = try JSONDecoder.controlPlane.decode(ApplicationDTO.self, from: applicationJSON(
            title: "Software Development Engineer - US Federal", company: ""))
        XCTAssertEqual(titleOnly.title, application.title)
        XCTAssertTrue(titleOnly.company.isEmpty)
    }

    func testManuallyCompletedUnsupportedQuestionReadsAsSatisfied() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON) as? [String: Any])
        object["kind"] = "field"
        object["action"] = "manual_complete"
        object["provenance"] = "skipped"
        object["verification"] = "not_attempted"
        object["review_state"] = "not_required"
        object["reason"] = "unsupported_control"
        object["requiredness"] = "required"
        let entry = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(entry.requirednessLabel, "Required")
        XCTAssertEqual(entry.outcomeLabel, "Manually completed")
        XCTAssertEqual(entry.reasonLabel, "Unsupported for automation")
        object["review_state"] = "pending"
        let pending = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(pending.outcomeLabel, "Needs review")
    }

    func testHumanResolutionIsLabeledWithoutVerification() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON) as? [String: Any])
        object["kind"] = "field"
        object["action"] = "human_resolved"
        object["provenance"] = "human_provided"
        object["verification"] = "not_attempted"
        object["review_state"] = "not_required"
        object["report_group"] = "completed"
        object["is_blocking"] = false
        let entry = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(entry.outcomeLabel, "Resolved manually")
        XCTAssertEqual(entry.sourceLabel, "Human provided")
        XCTAssertEqual(entry.verification, "not_attempted")
        let line = try XCTUnwrap(String(data: ControlOperation.resolveField("field-a")
            .encodedLine(id: "request-a"), encoding: .utf8))
        XCTAssertTrue(line.contains("resolve_field"))
        XCTAssertTrue(line.contains("field-a"))
        XCTAssertFalse(line.contains("browser_click"))
    }

    func testFailedApplicationRecoveryContractAndFailureHistory() throws {
        let line = try XCTUnwrap(String(data: ControlOperation.recoverApplication("task-a")
            .encodedLine(id: "request-a"), encoding: .utf8))
        XCTAssertTrue(line.contains("recover_application"))
        XCTAssertTrue(line.contains("task-a"))
        XCTAssertFalse(line.contains("browser_click"))
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: applicationJSON(status: "failed",
            resume: false)) as? [String: Any])
        object["recovery_available"] = true
        object["failure_events"] = [["reason": "browser_error", "stage": "observe_snapshot",
            "category": "timeout", "detail": "Managed browser operation timed out",
            "mode": "run", "occurred_at": "2026-10-01T00:00:00Z"]]
        let application = try JSONDecoder.controlPlane.decode(ApplicationDTO.self,
            from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(application.statusLabel, "FAILED")
        XCTAssertEqual(application.recoveryAvailable, true)
        XCTAssertEqual(application.failureEvents?.first?.stage, "observe_snapshot")
        XCTAssertEqual(application.failureEvents?.first?.category, "timeout")
    }

    func testCurrentReportPriorityAndOutcomeAreBackendDriven() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON) as? [String: Any])
        object["kind"] = "field"
        object["report_group"] = "needs_attention"
        object["is_blocking"] = true
        object["requires_user_action"] = true
        object["is_current_page"] = true
        let blocking = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(blocking.reportGroup, "needs_attention")
        XCTAssertEqual(blocking.isBlocking, true)
        XCTAssertEqual(blocking.outcomeLabel, "Needs your input")
        object["report_group"] = "needs_review"
        object["is_blocking"] = false
        object["action"] = "optional_skipped"
        object["review_state"] = "not_required"
        let optional = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(optional.outcomeLabel, "Unanswered")
        object["report_group"] = "completed"
        object["action"] = "confirmed_trusted"
        let verified = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(verified.outcomeLabel, "Filled and verified")
    }

    func testTechnicalReportCategoryKeepsQuestionsInMainReport() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON) as? [String: Any])
        object["kind"] = "field"
        object["requiredness"] = "required"
        object["semantic_key"] = "direct_source_url"
        object["category"] = "technical"
        let source = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertTrue(source.isTechnicalDetail)
        object["semantic_key"] = "page_classification"
        let classification = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertTrue(classification.isTechnicalDetail)
        object["semantic_key"] = "personal.first_name"
        object["category"] = "application"
        let question = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertFalse(question.isTechnicalDetail)
        XCTAssertEqual(question.requirednessLabel, "Required")
    }

    func testBackendReviewPhasesDriveVisibleLabelsAndSubmissionAvailability() throws {
        let cases: [(String, Bool, String?, Int, Bool, Bool, String)] = [
            ("ready_for_review", true, "needs_review", 1, false, false, "Needs Review"),
            ("ready_for_review", true, "ready_for_final_review", 0, false, false, "Ready for Final Review"),
            ("ready_for_review", true, "ready_to_submit", 0, true, true, "Ready to Submit"),
            ("submitted_by_human", false, "submitted", 0, true, false, "Submitted"),
            ("human_paused", false, nil, 0, false, false, "HUMAN PAUSED"),
        ]
        for (status, ready, phase, pending, checked, available, label) in cases {
            let application = try JSONDecoder.controlPlane.decode(
                ApplicationDTO.self, from: applicationJSON(status: status, resume: false,
                    ready: ready, phase: phase, pending: pending, checked: checked,
                    recordSubmission: available))
            XCTAssertEqual(application.reviewPhase, phase)
            XCTAssertEqual(application.statusLabel, label)
            XCTAssertEqual(application.recordSubmissionAvailable, available)
        }
    }

    func testRequestEncodingHasFixedMethodsAndNoAuthorizationFields() throws {
        let intake = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.startApplicationURL(" https://example.test/jobs/1 ").encodedLine(id: "0")) as? [String: Any])
        XCTAssertEqual(intake["method"] as? String, "start_application_url")
        XCTAssertEqual(intake["params"] as? [String: String], ["url": " https://example.test/jobs/1 "])
        let resume = try XCTUnwrap(String(data: ControlOperation.resumeApplication("task-a").encodedLine(id: "1"), encoding: .utf8))
        XCTAssertTrue(resume.hasSuffix("\n"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(resume.utf8)) as? [String: Any])
        XCTAssertEqual(object["method"] as? String, "resume_application")
        XCTAssertEqual(object["params"] as? [String: String], ["task_id": "task-a"])
        let archive = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.archiveApplication("task-a").encodedLine(id: "archive")) as? [String: Any])
        XCTAssertEqual(archive["method"] as? String, "archive_application")
        let open = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.openApplication("task-a").encodedLine(id: "open")) as? [String: Any])
        XCTAssertEqual(open["method"] as? String, "open_application")
        XCTAssertEqual(open["params"] as? [String: String], ["task_id": "task-a"])
        let diagnostic = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.diagnoseCurrentFields("task-a").encodedLine(id: "diagnostic")) as? [String: Any])
        XCTAssertEqual(diagnostic["method"] as? String, "diagnose_current_fields")
        XCTAssertEqual(diagnostic["params"] as? [String: String], ["task_id": "task-a"])
        XCTAssertNil(object["actor"])
        XCTAssertNil(object["action"])
        let foreground = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.bringWindowToFront("task-a").encodedLine(id: "2")) as? [String: Any])
        XCTAssertEqual(foreground["method"] as? String, "bring_window_to_front")
        XCTAssertNotEqual(foreground["method"] as? String, object["method"] as? String)
        let record = try XCTUnwrap(JSONSerialization.jsonObject(
            with: ControlOperation.recordSubmission("task-a").encodedLine(id: "3")) as? [String: Any])
        XCTAssertEqual(record["method"] as? String, "record_submission")
        XCTAssertEqual(record["params"] as? [String: String], ["task_id": "task-a"])
        XCTAssertNil(record["actor"])
        XCTAssertNil(record["action"])
    }

    @MainActor
    func testSelectionDoesNotForegroundButExplicitCommandDoes() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        var operations = await fake.operations
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        XCTAssertFalse(operations.contains(.openApplication("task-a")))
        await store.openApplication("task-a")
        operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .openApplication("task-a") }.count, 1)
        await store.bringWindowToFront("task-a")
        operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .bringWindowToFront("task-a") }.count, 1)
    }

    @MainActor
    func testDetailPendingCountComesFromTheSameReportAsVisibleCards() async throws {
        let fake = FakeClient()
        await fake.setApplicationData(applicationJSON(pending: 4))
        let template = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON)
            as? [String: Any])
        let cards: [[String: Any]] = (0..<3).map { index in
            var card = template
            card["entry_id"] = "field-\(index)"
            card["visible_label"] = "Question \(index + 1)"
            card["kind"] = "field"
            card["category"] = "application"
            card["report_group"] = "needs_review"
            return card
        }
        await fake.setReportData(try JSONSerialization.data(withJSONObject: [
            "task_id": "task-a", "entries": cards, "pending_review_count": 3]))
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        XCTAssertEqual(store.selectedApplication?.pendingReviewCount, 4)
        XCTAssertEqual(store.report.count, 3)
        XCTAssertEqual(store.reportPendingReviewCount, 3)
        XCTAssertEqual(store.visibleNeedsAttentionCount, 0)
        XCTAssertEqual(store.visibleNeedsReviewCount, 3)
    }

    @MainActor
    func testDirectURLCommandRefreshesAndSelectsCreatedApplication() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.startApplicationURL("https://example.test/jobs/1")
        let operations = await fake.operations
        XCTAssertTrue(operations.contains(.startApplicationURL("https://example.test/jobs/1")))
        XCTAssertEqual(store.selectedTaskId, "task-a")
        XCTAssertEqual(store.selectedRunId, "run-a")
        XCTAssertEqual(store.selectedApplication?.id, "task-a")
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
    }

    @MainActor
    func testRedactedDiagnosticCommandDoesNotTriggerBrowserAction() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        let clipboard = FakeDiagnosticClipboard()
        await store.reconnect()
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        let text = clipboard.writes.first
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .diagnoseCurrentFields("task-a") }.count, 1)
        XCTAssertFalse(operations.contains(.openApplication("task-a")))
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        XCTAssertTrue(text?.contains("[redacted]") == true)
        XCTAssertTrue(text?.contains("domRecoveredFieldCount") == true)
        XCTAssertTrue(text?.contains("dom_fallback") == true)
        XCTAssertFalse(text?.contains("California") == true)
        XCTAssertEqual(store.diagnosticStatus, .copied(1))
        XCTAssertTrue(text?.contains("generation-1") == true)
        XCTAssertTrue(text?.contains("\"manualResolution\" : \"human_attested\"") == true)
    }

    @MainActor
    func testRepeatedMarkResolvedWhileInFlightAttestsOnlyTheFirstField() async throws {
        let fake = FakeClient()
        await fake.setResolveDelay(.milliseconds(120))
        let store = AppStore(client: fake)
        let first = Task { await store.resolveField("field-a") }
        await Task.yield()
        XCTAssertEqual(store.resolvingEntryId, "field-a")
        // A second click lands on whichever card moved under the pointer.
        await store.resolveField("field-b")
        await first.value
        XCTAssertNil(store.resolvingEntryId)
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .resolveField("field-a") }.count, 1)
        XCTAssertFalse(operations.contains(.resolveField("field-b")))
        XCTAssertFalse(operations.contains(.recordSubmission("task-a")))
    }

    @MainActor
    func testUndoMarkResolvedIsOneFieldCommandWithoutBrowserAction() async throws {
        let line = try XCTUnwrap(String(data: ControlOperation.undoFieldResolution("field-a")
            .encodedLine(id: "request-a"), encoding: .utf8))
        XCTAssertTrue(line.contains("undo_field_resolution"))
        XCTAssertTrue(line.contains("\"entry_id\":\"field-a\""))
        XCTAssertFalse(line.contains("browser_"))
        let fake = FakeClient()
        await fake.setResolveDelay(.milliseconds(120))
        let store = AppStore(client: fake)
        let first = Task { await store.undoFieldResolution("field-a") }
        await Task.yield()
        // Resolve and Undo share one in-flight guard.
        await store.undoFieldResolution("field-b")
        await store.resolveField("field-c")
        await first.value
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .undoFieldResolution("field-a") }.count, 1)
        XCTAssertFalse(operations.contains(.undoFieldResolution("field-b")))
        XCTAssertFalse(operations.contains(.resolveField("field-c")))
        XCTAssertFalse(operations.contains(.openApplication("task-a")))
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        XCTAssertFalse(operations.contains(.resumeApplication("task-a")))
        XCTAssertFalse(operations.contains(.recordSubmission("task-a")))
    }

    func testHumanAttestationIsNotPresentedAsBrowserVerification() throws {
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: reportEntryJSON) as? [String: Any])
        object["kind"] = "field"
        object["action"] = "human_resolved"
        object["provenance"] = "human_provided"
        object["verification"] = "not_attempted"
        object["review_state"] = "not_required"
        object["reason"] = "human_attested"
        object["report_group"] = "completed"
        let entry = try JSONDecoder.controlPlane.decode(
            ReportEntryDTO.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertEqual(entry.outcomeLabel, "Resolved manually")
        XCTAssertEqual(entry.reasonLabel, "You marked this resolved · not verified on the employer page")
        XCTAssertNotEqual(entry.outcomeLabel, "Filled and verified")
    }

    @MainActor
    func testDiagnosticStartsWithoutPollingAndIgnoresDoubleClick() async throws {
        let fake = FakeClient()
        await fake.setDiagnosticDelay(.milliseconds(120))
        let store = AppStore(client: fake)
        let clipboard = FakeDiagnosticClipboard()
        let first = Task { await store.copyFieldDiagnostic("task-a", clipboard: clipboard) }
        await Task.yield()
        XCTAssertEqual(store.diagnosticStatus, .capturing)
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .capturing)
        for _ in 0..<100 {
            if await fake.operations.contains(.diagnoseCurrentFields("task-a")) { break }
            await Task.yield()
        }
        let started = await fake.operations
        XCTAssertEqual(started.filter { $0 == .diagnoseCurrentFields("task-a") }.count, 1)
        await first.value
        XCTAssertEqual(store.diagnosticStatus, .copied(1))
        XCTAssertEqual(clipboard.writes.count, 1)
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(clipboard.writes.count, 2)
        XCTAssertTrue(clipboard.writes[0].contains("generation-1"))
        XCTAssertTrue(clipboard.writes[1].contains("generation-2"))
    }

    @MainActor
    func testDiagnosticPollDoesNotResetCaptureAndFailureNeverCopiesOldPayload() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        let clipboard = FakeDiagnosticClipboard()
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .copied(1))
        await fake.setDiagnosticDelay(.milliseconds(80))
        let second = Task { await store.copyFieldDiagnostic("task-a", clipboard: clipboard) }
        await Task.yield()
        XCTAssertEqual(store.diagnosticStatus, .capturing)
        await store.refresh()
        XCTAssertEqual(store.diagnosticStatus, .capturing)
        await fake.setFailure(.unavailable)
        await second.value
        XCTAssertEqual(store.diagnosticStatus, .failed("backend unavailable"))
        XCTAssertEqual(clipboard.writes.count, 1)
    }

    @MainActor
    func testDiagnosticTimeoutRejectsLateResponseAndRecovers() async {
        let fake = FakeClient()
        await fake.setDiagnosticDelay(.milliseconds(100))
        let store = AppStore(client: fake, diagnosticTimeout: .milliseconds(20))
        let clipboard = FakeDiagnosticClipboard()
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .failed("timed out"))
        XCTAssertTrue(clipboard.writes.isEmpty)
        await fake.setDiagnosticDelay(nil)
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .copied(1))
        XCTAssertEqual(clipboard.writes.count, 1)
        try? await Task.sleep(for: .milliseconds(120))
        XCTAssertEqual(clipboard.writes.count, 1)
    }

    @MainActor
    func testDiagnosticClipboardFailureKeepsCaptureWithoutRetry() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        let clipboard = FakeDiagnosticClipboard()
        clipboard.succeeds = false
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .failed("clipboard error"))
        XCTAssertNotNil(store.lastCapturedDiagnostic)
        XCTAssertEqual(clipboard.writes.count, 1)
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .diagnoseCurrentFields("task-a") }.count, 1)
    }

    @MainActor
    func testDiagnosticCopiedStatusPersistsThenResetsWithoutClearingFailure() async {
        let fake = FakeClient()
        let store = AppStore(client: fake, diagnosticSuccessDuration: .milliseconds(60))
        let clipboard = FakeDiagnosticClipboard()
        XCTAssertEqual(store.diagnosticStatus, .idle)
        XCTAssertEqual(store.diagnosticStatus.label, "Ready")
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .copied(1))
        XCTAssertEqual(store.diagnosticStatus.label, "Copied — 1 fields")
        try? await Task.sleep(for: .milliseconds(20))
        XCTAssertEqual(store.diagnosticStatus, .copied(1))
        try? await Task.sleep(for: .milliseconds(70))
        XCTAssertEqual(store.diagnosticStatus, .idle)
        await fake.setFailure(.unavailable)
        await store.copyFieldDiagnostic("task-a", clipboard: clipboard)
        XCTAssertEqual(store.diagnosticStatus, .failed("backend unavailable"))
        try? await Task.sleep(for: .milliseconds(70))
        XCTAssertEqual(store.diagnosticStatus, .failed("backend unavailable"))
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
        XCTAssertEqual(store.selectedApplication?.status, "queued")
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
    func testAuthPauseResumeAcknowledgesOnceAndDoesNotHoldOtherControls() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        XCTAssertEqual(store.selectedApplication?.status, "human_paused")
        await fake.setResumeDelay(.milliseconds(100))
        let first = Task { await store.resume("task-a") }
        await Task.yield()
        XCTAssertEqual(store.resumingTaskId, "task-a")
        await store.resume("task-a")
        await store.bringWindowToFront("task-a")
        await first.value
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .resumeApplication("task-a") }.count, 1)
        XCTAssertEqual(operations.filter { $0 == .bringWindowToFront("task-a") }.count, 1)
        XCTAssertNil(store.resumingTaskId)
        XCTAssertEqual(store.selectedApplication?.status, "queued")
    }

    @MainActor
    func testStartAcknowledgesDurableTaskWithoutBlockingOnWorkerRefresh() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        let before = await fake.operations.filter { $0 == .listRuns }.count
        await fake.setStartDelay(.milliseconds(100))
        let first = Task { await store.startApplicationURL("https://example.test/apply") }
        await Task.yield()
        XCTAssertTrue(store.startingApplicationURL)
        await store.startApplicationURL("https://example.test/apply")
        await first.value
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .startApplicationURL("https://example.test/apply") }.count, 1)
        XCTAssertEqual(operations.filter { $0 == .listRuns }.count, before)
        XCTAssertEqual(store.selectedTaskId, "task-a")
        XCTAssertFalse(store.startingApplicationURL)
    }

    @MainActor
    func testResumeErrorClearsBusyStateForAnotherAction() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        await fake.setFailure(.requestTimeout)
        await store.resume("task-a")
        XCTAssertNil(store.resumingTaskId)
        XCTAssertTrue(store.errorMessage?.contains("did not respond") == true)
        await fake.setFailure(nil)
        await store.bringWindowToFront("task-a")
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .bringWindowToFront("task-a") }.count, 1)
        XCTAssertNil(store.errorMessage)
    }

    @MainActor
    func testForegroundRequestClearsBusyStateAfterTimeoutAndPreventsDuplicateClick() async {
        let fake = FakeClient()
        let store = AppStore(client: fake)
        await store.reconnect()
        await fake.setForegroundDelay(.milliseconds(100))
        await fake.setFailure(.requestTimeout)
        let first = Task { await store.bringWindowToFront("task-a") }
        await Task.yield()
        XCTAssertEqual(store.foregroundingTaskId, "task-a")
        await store.bringWindowToFront("task-a")
        await first.value
        XCTAssertNil(store.foregroundingTaskId)
        XCTAssertTrue(store.errorMessage?.contains("did not respond") == true)
        await fake.setFailure(nil)
        await store.bringWindowToFront("task-a")
        let operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .bringWindowToFront("task-a") }.count, 2)
        XCTAssertNil(store.errorMessage)
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
        XCTAssertFalse(operations.contains(.openApplication("task-a")))
        XCTAssertFalse(operations.contains(.bringWindowToFront("task-a")))
        XCTAssertFalse(operations.contains(.markFinalReviewChecked("task-a")))
        XCTAssertFalse(operations.contains(.recordSubmission("task-a")))
    }

    @MainActor
    func testRecordSubmissionIsExplicitAndRefreshesSubmittedState() async {
        let fake = FakeClient()
        await fake.setApplicationData(applicationJSON(status: "ready_for_review", resume: false,
            ready: true, phase: "ready_to_submit", checked: true, recordSubmission: true))
        let store = AppStore(client: fake)
        await store.reconnect()
        await store.selectApplication("task-a")
        XCTAssertEqual(store.selectedApplication?.statusLabel, "Ready to Submit")
        XCTAssertEqual(store.selectedApplication?.recordSubmissionAvailable, true)
        var operations = await fake.operations
        XCTAssertFalse(operations.contains(.recordSubmission("task-a")))
        await store.refresh()
        operations = await fake.operations
        XCTAssertFalse(operations.contains(.recordSubmission("task-a")))
        await store.recordSubmission("task-a")
        operations = await fake.operations
        XCTAssertEqual(operations.filter { $0 == .recordSubmission("task-a") }.count, 1)
        XCTAssertEqual(store.selectedApplication?.statusLabel, "Submitted")
        XCTAssertEqual(store.selectedApplication?.status, "submitted_by_human")
        XCTAssertEqual(store.selectedApplication?.recordSubmissionAvailable, false)
        XCTAssertEqual(store.attention.count, 0)
        XCTAssertEqual(store.applications.count, 1)
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
