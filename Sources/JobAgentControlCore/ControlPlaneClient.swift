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
        case .transport: "Backend connection failed. Use Reconnect to try again."
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

    public init(pythonExecutable: String, repositoryRoot: URL, databasePath: String) {
        self.pythonExecutable = pythonExecutable
        self.repositoryRoot = repositoryRoot
        self.databasePath = databasePath
    }

    public static func fromEnvironment(_ environment: [String: String] = ProcessInfo.processInfo.environment) -> Self? {
        guard let root = environment["JOBAGENT_BACKEND_ROOT"], !root.isEmpty,
              let database = environment["JOBAGENT_DB_PATH"], !database.isEmpty else { return nil }
        return Self(pythonExecutable: environment["JOBAGENT_PYTHON"] ?? "/usr/bin/python3",
                    repositoryRoot: URL(fileURLWithPath: root, isDirectory: true),
                    databasePath: database)
    }
}
