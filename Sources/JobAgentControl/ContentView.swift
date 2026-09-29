import SwiftUI
import JobAgentControlCore

struct ContentView: View {
    @ObservedObject var store: AppStore
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        NavigationSplitView {
            List {
                Section("Runs") {
                    ForEach(store.runs) { run in
                        Button {
                            store.showsAttention = false
                            store.selectedRunId = run.id
                        } label: {
                            VStack(alignment: .leading) {
                                Text(run.requestedSources.joined(separator: ", "))
                                Text("\(run.status) · \(run.queuedCount) queued")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                Section("Review") {
                    Button {
                        store.showsAttention = true
                    } label: {
                        Label("Needs Attention (\(store.attention.count))", systemImage: "exclamationmark.circle")
                    }
                    .buttonStyle(.plain)
                }
            }
            .navigationTitle("Job Agent")
        } content: {
            List {
                ForEach(store.visibleApplications) { application in
                    Button {
                        // Selection only requests detail; foregrounding has its own button.
                        Task { await store.selectApplication(application.id) }
                    } label: {
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(application.title).font(.headline)
                                Text(application.company).foregroundStyle(.secondary)
                            }
                            Spacer()
                            StatusBadge(application: application)
                        }
                        .padding(.vertical, 5)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 7) {
                    Text(application.title).font(.largeTitle.bold())
                    Text(application.company).font(.title3).foregroundStyle(.secondary)
                    StatusBadge(application: application)
                    LabeledContent("Ownership", value: application.ownership)
                    LabeledContent("Step", value: application.currentPageOrStep ?? "—")
                    if let blocker = application.blocker { LabeledContent("Blocker", value: blocker) }
                    if let reason = application.failureReason { LabeledContent("Failure", value: reason) }
                    LabeledContent("Window", value: application.windowAvailable ? "Available" : "Unavailable")
                    LabeledContent("Pending reviews", value: "\(application.pendingReviewCount)")
                }

                HStack {
                    if application.resumeAvailable {
                        Button("Resume") { Task { await store.resume(application.id) } }
                    }
                    Button("Bring Window to Front") {
                        Task { await store.bringWindowToFront(application.id) }
                    }
                }

                Divider()
                Text("Application Report").font(.title2.bold())
                ForEach(store.report.filter { $0.kind != "narrative" }) { entry in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(entry.visibleLabel).font(.headline)
                        Text("\(entry.pageOrStep ?? "Unknown step") · \(entry.semanticKey ?? "No semantic key")")
                            .font(.caption).foregroundStyle(.secondary)
                        Text("\(entry.provenance) · \(entry.action) · \(entry.verification) · \(entry.reviewState)")
                            .font(.caption)
                        if let reason = entry.reason { Text(reason).font(.caption).foregroundStyle(.secondary) }
                        if entry.reviewState == "pending" && application.readyForReview {
                            Button("Mark Item Reviewed") { Task { await store.reviewEntry(entry.id) } }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10).background(.quaternary.opacity(0.35))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
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
