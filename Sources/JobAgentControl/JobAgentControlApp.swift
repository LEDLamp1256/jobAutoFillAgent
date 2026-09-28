import SwiftUI
import JobAgentControlCore

@main
struct JobAgentControlApp: App {
    @StateObject private var store: AppStore

    init() {
        let client = BackendConfiguration.fromEnvironment().map { ControlPlaneProcess(configuration: $0) }
        _store = StateObject(wrappedValue: AppStore(client: client))
    }

    var body: some Scene {
        WindowGroup("Job Application Agent") {
            ContentView(store: store)
                .frame(minWidth: 960, minHeight: 640)
        }
    }
}
