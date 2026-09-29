import Foundation

public enum BackendConnection: Equatable, Sendable {
    case starting
    case connected
    case disconnected
    case error(String)

    public var label: String {
        switch self {
        case .starting: "Starting"
        case .connected: "Connected"
        case .disconnected: "Disconnected"
        case .error: "Error"
        }
    }
}

public enum ControlClientError: Error, LocalizedError, Equatable, Sendable {
    case unavailable
    case protocolMismatch
    case backend(BackendErrorDTO)
    case transport(String)

    public var errorDescription: String? {
        switch self {
        case .unavailable: "Backend is unavailable. Use Reconnect to try again."
        case .protocolMismatch: "Backend response did not match the request."
        case .backend(let error): "\(error.code): \(error.message)"
        case .transport(let message): message
        }
    }
}

public protocol ControlPlaneClient: Sendable {
    func send<T: Decodable & Sendable>(_ operation: ControlOperation, as type: T.Type) async throws -> T
    func restart() async throws
    func connectionState() async -> BackendConnection
}

public struct BackendConfiguration: Sendable {
    public let pythonExecutable: String
    public let repositoryRoot: URL
    public let databasePath: String
    public let configurationPath: String?
    public let mcpCLIPath: String?
    public let nodeExecutable: String?
    public let supportDirectory: URL?

    public init(pythonExecutable: String, repositoryRoot: URL, databasePath: String,
                configurationPath: String? = nil, mcpCLIPath: String? = nil,
                nodeExecutable: String? = nil, supportDirectory: URL? = nil) {
        self.pythonExecutable = pythonExecutable
        self.repositoryRoot = repositoryRoot
        self.databasePath = databasePath
        self.configurationPath = configurationPath
        self.mcpCLIPath = mcpCLIPath
        self.nodeExecutable = nodeExecutable
        self.supportDirectory = supportDirectory
    }

    public static func fromEnvironment(
        _ environment: [String: String] = ProcessInfo.processInfo.environment,
        bundleResources: URL? = Bundle.main.resourceURL,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        isBundledApplication: Bool = Bundle.main.bundleURL.pathExtension == "app"
    ) -> Self? {
        if let root = environment["JOBAGENT_BACKEND_ROOT"], !root.isEmpty,
           let database = environment["JOBAGENT_DB_PATH"], !database.isEmpty {
            return Self(pythonExecutable: environment["JOBAGENT_PYTHON"] ?? "/usr/bin/python3",
                        repositoryRoot: URL(fileURLWithPath: root, isDirectory: true),
                        databasePath: database)
        }
        guard isBundledApplication, let bundleResources else { return nil }
        let support = homeDirectory.appendingPathComponent(
            "Library/Application Support/Job Application Agent", isDirectory: true)
        let runtime = support.appendingPathComponent("runtime", isDirectory: true)
        return Self(
            pythonExecutable: runtime.appendingPathComponent("python/bin/python").path,
            repositoryRoot: bundleResources.appendingPathComponent("Backend", isDirectory: true),
            databasePath: support.appendingPathComponent("applications.sqlite3").path,
            configurationPath: support.appendingPathComponent("config.json").path,
            mcpCLIPath: runtime.appendingPathComponent(
                "playwright-mcp/node_modules/@playwright/mcp/cli.js").path,
            nodeExecutable: runtime.appendingPathComponent("bin/node").path,
            supportDirectory: support)
    }
}
