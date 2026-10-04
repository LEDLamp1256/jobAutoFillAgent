import AppKit
import SwiftUI
import JobAgentControlCore

@MainActor private struct SystemDiagnosticClipboard: DiagnosticClipboard {
    func write(_ text: String) -> Bool {
        NSPasteboard.general.clearContents()
        return NSPasteboard.general.setString(text, forType: .string)
    }
}

struct ContentView: View {
    @ObservedObject var store: AppStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var directURL = ""
    @State private var showsRunHistory = false
    @State private var pendingArchive: ApplicationDTO?

    var body: some View {
        NavigationSplitView {
            List {
                Section("Start Application") {
                    TextField("Job posting or application URL", text: $directURL)
                        .textFieldStyle(.roundedBorder)
                    Button(store.startingApplicationURL ? "Starting…" : "Start Application") {
                        let url = directURL
                        Task { await store.startApplicationURL(url) }
                    }
                    .disabled(store.connection != .connected || store.startingApplicationURL ||
                              directURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                Section("Review") {
                    Button {
                        store.showsAttention = true
                    } label: {
                        Label("Needs Attention (\(store.attention.count))", systemImage: "exclamationmark.circle")
                    }
                    .buttonStyle(.plain)
                }
                Section {
                    DisclosureGroup("Run History (\(store.runs.count))", isExpanded: $showsRunHistory) {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 6) {
                                Button("All Applications") {
                                    store.showsAttention = false
                                    store.selectedRunId = nil
                                }
                                .buttonStyle(.plain)
                                ForEach(store.runs) { run in
                                    Button {
                                        store.showsAttention = false
                                        store.selectedRunId = run.id
                                    } label: {
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(run.historyLabel(applications: store.applications))
                                            Text("\(run.createdAt.prefix(10)) · \(run.taskHistoryLabel)")
                                                .font(.caption).foregroundStyle(.secondary)
                                        }
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                        .frame(maxHeight: 180)
                    }
                }
            }
            .navigationTitle("Job Agent")
        } content: {
            List {
                ForEach(store.visibleApplications) { application in
                    HStack(spacing: 8) {
                    Button {
                        // Selection only requests detail; foregrounding has its own button.
                        Task { await store.selectApplication(application.id) }
                    } label: {
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(application.title).font(.headline)
                                if !application.company.isEmpty {
                                    Text(application.company).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            StatusBadge(application: application)
                        }
                        .padding(.vertical, 5)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    Button {
                        pendingArchive = application
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.plain)
                    .help(application.status == "queued" ? "Remove from queue" : "Archive local application")
                    .accessibilityLabel(application.status == "queued" ? "Remove from queue" : "Archive local application")
                    .disabled(["launching", "authenticating", "filling", "advancing"].contains(application.status))
                    }
                    .listRowBackground(store.selectedTaskId == application.id ? Color.accentColor.opacity(0.12) : Color.clear)
                }
            }
            .navigationTitle(store.showsAttention ? "Needs Attention" : "Applications")
            .overlay {
                if store.visibleApplications.isEmpty {
                    ContentUnavailableView("No Applications", systemImage: "tray")
                }
            }
        } detail: {
            if let application = store.selectedApplication {
                ApplicationDetail(store: store, application: application)
            } else {
                ContentUnavailableView("Select an Application", systemImage: "doc.text.magnifyingglass")
            }
        }
        .confirmationDialog(
            pendingArchive?.status == "queued" ? "Remove from queue?" : "Archive local application?",
            isPresented: Binding(get: { pendingArchive != nil },
                                 set: { if !$0 { pendingArchive = nil } }),
            presenting: pendingArchive
        ) { application in
            Button(application.status == "queued" ? "Remove from queue" : "Archive local record",
                   role: .destructive) {
                pendingArchive = nil
                Task { await store.archiveApplication(application.id) }
            }
        } message: { _ in
            Text("This only changes the local application list. It does not change or withdraw anything on the employer site.")
        }
        .toolbar {
            ToolbarItemGroup {
                Text(store.connection.label)
                    .padding(.leading, 8)
                    .foregroundStyle(store.connection == .connected ? .green : .secondary)
                Button {
                    Task { await store.refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise").labelStyle(.titleAndIcon)
                }
                .disabled(store.connection != .connected || store.isLoading)
                .accessibilityLabel("Refresh application data")
                .help("Reload data from the connected backend. Does not restart Python.")
                Button {
                    Task { await store.reconnect() }
                } label: {
                    Label("Reconnect Backend", systemImage: "bolt.horizontal.circle").labelStyle(.titleAndIcon)
                }
                .disabled(!store.canReconnect)
                .accessibilityLabel("Reconnect Backend")
                .help("Restart the Python backend and reload saved data from SQLite.")
            }
        }
        .safeAreaInset(edge: .bottom) {
            if let error = store.errorMessage {
                Text(error).font(.callout).foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10).background(.regularMaterial)
            }
        }
        .task { await store.reconnect() }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 20_000_000_000) }
                catch { break }
                await store.refresh()
            }
        }
    }

}

private struct StatusBadge: View {
    let application: ApplicationDTO

    var body: some View {
        Text(application.statusLabel)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 7).padding(.vertical, 4)
            .background(application.needsAttention ? Color.orange.opacity(0.2) : Color.secondary.opacity(0.12))
            .clipShape(Capsule())
    }
}

private struct ApplicationDetail: View {
    @ObservedObject var store: AppStore
    let application: ApplicationDTO
    @State private var showingSubmissionConfirmation = false
    @State private var technicalDetailsExpanded = false
    @State private var failureHistoryExpanded = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 7) {
                    Text(application.title).font(.largeTitle.bold())
                    if !application.company.isEmpty {
                        Text(application.company).font(.title3).foregroundStyle(.secondary)
                    }
                    StatusBadge(application: application)
                    LabeledContent("Ownership", value: application.ownership)
                    LabeledContent("Step", value: application.currentPageOrStep ?? "—")
                    if let sourceURL = application.sourceUrl { LabeledContent("Source URL", value: sourceURL) }
                    if let liveURL = application.liveUrl { LabeledContent("Live URL", value: liveURL) }
                    if let classification = application.classification {
                        LabeledContent("Page", value: classification.replacingOccurrences(of: "_", with: " "))
                    }
                    if let blocker = application.blockerLabel {
                        LabeledContent("Needs attention", value: blocker)
                    }
                    if let reason = application.failureReason { LabeledContent("Failure", value: reason) }
                    if let failures = application.failureEvents, !failures.isEmpty {
                        Button {
                            failureHistoryExpanded.toggle()
                        } label: {
                            HStack {
                                Text("Failure History (\(failures.count))")
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .rotationEffect(.degrees(failureHistoryExpanded ? 90 : 0))
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityValue(failureHistoryExpanded ? "Expanded" : "Collapsed")
                        if failureHistoryExpanded {
                            ForEach(Array(failures.enumerated()), id: \.offset) { _, failure in
                                VStack(alignment: .leading, spacing: 3) {
                                    Text("\(failure.mode.capitalized) · \(failure.stage)")
                                        .font(.caption.weight(.semibold))
                                    Text("\(failure.category): \(failure.detail)").font(.caption)
                                    Text(failure.occurredAt).font(.caption2).foregroundStyle(.secondary)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                    LabeledContent("Window", value: application.windowAvailable ? "Available" : "Unavailable")
                    LabeledContent("Needs Attention", value: "\(store.visibleNeedsAttentionCount)")
                    LabeledContent("Needs Review", value: "\(store.visibleNeedsReviewCount)")
                }

                HStack {
                    if application.recoveryAvailable == true {
                        Button(store.recoveringTaskId == application.id ? "Recovering…" : "Recover Application") {
                            Task { await store.recoverApplication(application.id) }
                        }
                        .disabled(store.recoveringTaskId != nil)
                    }
                    if application.resumeAvailable {
                        Button(store.resumingTaskId == application.id ? "Resuming…" : "Resume") {
                            Task { await store.resume(application.id) }
                        }
                        .disabled(store.resumingTaskId != nil)
                    }
                    if application.resumeAvailable && !application.windowAvailable {
                        Button("Open Application") {
                            Task { await store.openApplication(application.id) }
                        }
                    }
                    Button(store.foregroundingTaskId == application.id ?
                           "Bringing Window…" : "Bring Window to Front") {
                        Task { await store.bringWindowToFront(application.id) }
                    }
                    .disabled(!application.windowAvailable || store.foregroundingTaskId != nil)
                }

                Divider()
                Text("Application Report").font(.title2.bold())
                let blocking = store.report.filter {
                    $0.kind == "field" && $0.reportGroup == "needs_attention"
                }
                if !blocking.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Needs your input").font(.headline)
                        Text(blocking.count == 1 ?
                             "1 field needs your input before this application can continue." :
                             "\(blocking.count) fields need your input before this application can continue.")
                        if let reason = application.blockerLabel {
                            Text(reason).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10).background(Color.orange.opacity(0.15))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                } else if let reason = application.blockerLabel {
                    Text(reason)
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10).background(Color.orange.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                }
                ForEach([("needs_attention", "Needs Your Attention"),
                         ("needs_review", "Needs Review"),
                         ("completed", "Completed")], id: \.0) { group in
                    let entries = store.report.filter { $0.kind != "narrative" &&
                        $0.reportGroup == group.0 }
                    if !entries.isEmpty {
                        Text(group.1).font(.headline)
                        ForEach(entries) { entry in
                            ApplicationFieldCard(entry: entry, canReview: application.readyForReview,
                                                 resolveDisabled: store.resolvingEntryId != nil,
                                                 approve: { Task { await store.reviewEntry(entry.id) } },
                                                 resolve: { Task { await store.resolveField(entry.id) } },
                                                 undoResolve: { Task { await store.undoFieldResolution(entry.id) } })
                        }
                    }
                }

                Divider()
                Text("Narrative Review").font(.title2.bold())
                if store.narratives.isEmpty {
                    Text("No narrative entries").foregroundStyle(.secondary)
                }
                ForEach(NarrativePresentation.items(from: store.narratives)) { item in
                    VStack(alignment: .leading, spacing: 8) {
                        if !item.history.isEmpty {
                            Text("Current Answer").font(.headline)
                        }
                        NarrativeReviewCard(entry: item.current, canReview: application.readyForReview,
                                            approve: { Task { await store.reviewEntry(item.current.id) } },
                                            replace: { text in Task { await store.replaceNarrative(item.current.id, with: text) } })
                        if !item.history.isEmpty {
                            DisclosureGroup("Original AI Draft · History") {
                                ForEach(item.history) { original in
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(original.visibleLabel).font(.headline)
                                        Text("\(original.provenance) · \(original.reviewState) · replaced by owner")
                                            .font(.caption).foregroundStyle(.secondary)
                                        Text(original.narrativeText ?? "No text").textSelection(.enabled)
                                    }
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.vertical, 6)
                                }
                            }
                            .font(.callout)
                        }
                    }
                }

                Divider()
                if application.finalReviewChecked {
                    Label("Final review checked", systemImage: "checkmark.circle")
                } else if application.finalReviewAvailable {
                    Button("Mark Final Review Complete") {
                        Task { await store.markFinalReviewChecked(application.id) }
                    }
                }
                Text("Final review complete does not submit the application.")
                    .font(.caption).foregroundStyle(.secondary)
                if application.recordSubmissionAvailable {
                    Button("Record as Submitted") {
                        showingSubmissionConfirmation = true
                    }
                    Text("Use this only after manually submitting on the employer website.")
                        .font(.caption).foregroundStyle(.secondary)
                }

                if store.report.contains(where: { $0.isTechnicalDetail }) {
                    Divider()
                    Button {
                        technicalDetailsExpanded.toggle()
                    } label: {
                        HStack {
                            Image(systemName: technicalDetailsExpanded ? "chevron.down" : "chevron.right")
                            Text("Technical Details")
                            Spacer(minLength: 0)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(technicalDetailsExpanded ? "Expanded" : "Collapsed")
                    if technicalDetailsExpanded {
                        Button(store.diagnosticStatus == .capturing
                               ? "Capturing Diagnostic…" : "Copy Redacted Field Diagnostic") {
                            Task { await store.copyFieldDiagnostic(application.id,
                                                                   clipboard: SystemDiagnosticClipboard()) }
                        }
                        .disabled(store.diagnosticStatus == .capturing)
                        HStack(spacing: 6) {
                            if store.diagnosticStatus == .capturing { ProgressView().controlSize(.small) }
                            Text(store.diagnosticStatus.label)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .accessibilityLabel("Diagnostic status: \(store.diagnosticStatus.label)")
                        Text("Reads the current managed page without changing answers. Copy contains no answer values.")
                            .font(.caption).foregroundStyle(.secondary)
                        ForEach(store.report.filter { $0.isTechnicalDetail }) { entry in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(entry.visibleLabel).font(.headline)
                                Text("\(entry.action.replacingOccurrences(of: "_", with: " ")) · \(entry.pageOrStep ?? "—")")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 5)
                        }
                    }
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle("Application")
        .confirmationDialog("Record application as submitted?",
                            isPresented: $showingSubmissionConfirmation,
                            titleVisibility: .visible) {
            Button("Record as Submitted") {
                Task { await store.recordSubmission(application.id) }
            }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("Only continue after you have manually submitted this application on the employer's website. Job Application Agent will record it as submitted but will not click Submit for you.")
        }
    }
}

private struct ApplicationFieldCard: View {
    let entry: ReportEntryDTO
    let canReview: Bool
    let resolveDisabled: Bool
    let approve: () -> Void
    let resolve: () -> Void
    let undoResolve: () -> Void
    @State private var confirmingResolve = false
    @State private var confirmingUndo = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(entry.visibleLabel).font(.headline)
                if entry.isBlocking == true {
                    Text("BLOCKING").font(.caption.bold()).foregroundStyle(.orange)
                }
            }
            if let requiredness = entry.requirednessLabel {
                Text(requiredness).font(.caption.weight(.semibold))
            }
            Text(entry.outcomeLabel).font(.subheadline.weight(.medium))
            if let step = entry.pageOrStep {
                Text(step).font(.caption).foregroundStyle(.secondary)
            }
            if let source = entry.sourceLabel, entry.verification == "verified" {
                Text("Source: \(source)").font(.caption).foregroundStyle(.secondary)
            }
            if let reason = entry.reasonLabel {
                Text(reason).font(.caption).foregroundStyle(.secondary)
            }
            if entry.reviewState == "pending" && canReview {
                Button("Mark Item Reviewed", action: approve)
            }
            if entry.isBlocking == true && entry.kind == "field" {
                Text("I checked this field on the employer page.")
                    .font(.caption).foregroundStyle(.secondary)
                // Attestation names one field and needs its own confirmation,
                // so clicks cannot carry over to the card that moves up next.
                Button("Mark Resolved") { confirmingResolve = true }
                    .disabled(resolveDisabled)
                    .confirmationDialog("Mark “\(entry.visibleLabel)” resolved?",
                                        isPresented: $confirmingResolve, titleVisibility: .visible) {
                        Button("Mark “\(entry.visibleLabel)” Resolved", action: resolve)
                        Button("Cancel", role: .cancel) {}
                    } message: {
                        Text("Only this field is marked resolved. The employer page is not changed or re-checked.")
                    }
            }
            if entry.action == "human_resolved" && entry.kind == "field" {
                Button("Undo Mark Resolved") { confirmingUndo = true }
                    .disabled(resolveDisabled)
                    .confirmationDialog(entry.undoDialogTitle,
                                        isPresented: $confirmingUndo, titleVisibility: .visible) {
                        Button("Undo Mark Resolved", action: undoResolve)
                        Button("Cancel", role: .cancel) {}
                    } message: {
                        Text(entry.undoDialogMessage)
                    }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10).background(.quaternary.opacity(0.35))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct NarrativeReviewCard: View {
    let entry: ReportEntryDTO
    let canReview: Bool
    let approve: () -> Void
    let replace: (String) -> Void
    @State private var draft: String

    init(entry: ReportEntryDTO, canReview: Bool, approve: @escaping () -> Void,
         replace: @escaping (String) -> Void) {
        self.entry = entry
        self.canReview = canReview
        self.approve = approve
        self.replace = replace
        _draft = State(initialValue: entry.narrativeText ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(entry.visibleLabel).font(.headline)
            Text("\(entry.pageOrStep ?? "Unknown step") · \(entry.provenance) · \(entry.reviewState)")
                .font(.caption).foregroundStyle(.secondary)
            if entry.provenance == "ai_draft_review" && entry.reviewState == "pending" && canReview {
                Text("AI draft").font(.caption.bold())
                Text(entry.narrativeText ?? "").textSelection(.enabled)
                TextEditor(text: $draft).frame(minHeight: 110)
                    .accessibilityLabel("Replacement for \(entry.visibleLabel)")
                HStack {
                    Button("Approve Draft", action: approve)
                    Button("Save Replacement") { replace(draft) }
                        .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ||
                                  draft == entry.narrativeText)
                }
            } else {
                Text(entry.narrativeText ?? "No text").textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12).background(.quaternary.opacity(0.35))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
