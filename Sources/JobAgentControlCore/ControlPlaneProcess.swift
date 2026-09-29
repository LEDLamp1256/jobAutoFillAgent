import Foundation

/// One backend child and one outstanding request at a time. Stdout is protocol only.
public actor ControlPlaneProcess: ControlPlaneClient {
    private let configuration: BackendConfiguration
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var currentState: BackendConnection = .disconnected

    public init(configuration: BackendConfiguration) {
        self.configuration = configuration
    }

    public func connectionState() -> BackendConnection {
        if let process, !process.isRunning, currentState == .connected {
            currentState = .disconnected
        }
        return currentState
    }

    public func restart() throws {
        stop()
        currentState = .starting
        let files = FileManager.default
        guard files.isExecutableFile(atPath: configuration.pythonExecutable) else {
            currentState = .error("Python runtime is missing")
            throw ControlClientError.transport("Python runtime is missing. Run Scripts/install-macos-app.sh from the repository.")
        }
        guard files.fileExists(atPath: configuration.repositoryRoot.appendingPathComponent(
            "jobagent/control_plane_stdio.py").path) else {
            currentState = .error("Bundled Python backend is missing")
            throw ControlClientError.transport("The app's Python backend is missing. Reinstall Job Application Agent.app.")
        }
        if let mcpCLIPath = configuration.mcpCLIPath,
           !files.fileExists(atPath: mcpCLIPath) {
            currentState = .error("Playwright MCP runtime is missing")
            throw ControlClientError.transport("Playwright MCP runtime is missing. Run Scripts/install-macos-app.sh from the repository.")
        }
        if let nodeExecutable = configuration.nodeExecutable,
           !files.isExecutableFile(atPath: nodeExecutable) {
            currentState = .error("Node runtime is missing")
            throw ControlClientError.transport("Node runtime is missing. Run Scripts/install-macos-app.sh from the repository.")
        }
        if let supportDirectory = configuration.supportDirectory {
            do {
                try files.createDirectory(at: supportDirectory, withIntermediateDirectories: true)
            } catch {
                currentState = .error("Application Support is unavailable")
                throw ControlClientError.transport("Cannot create Application Support for the application database. Check folder permissions.")
            }
        }
        let child = Process()
        child.executableURL = URL(fileURLWithPath: configuration.pythonExecutable)
        var arguments = ["-m", "jobagent.control_plane_stdio", "--db", configuration.databasePath]
        if let configurationPath = configuration.configurationPath {
            arguments += ["--config", configurationPath]
        }
        if let mcpCLIPath = configuration.mcpCLIPath {
            arguments += ["--mcp-cli", mcpCLIPath]
        }
        child.arguments = arguments
        child.currentDirectoryURL = configuration.repositoryRoot
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if let nodeExecutable = configuration.nodeExecutable {
            let directory = URL(fileURLWithPath: nodeExecutable).deletingLastPathComponent().path
            environment["PATH"] = directory + ":" + (environment["PATH"] ?? "/usr/bin:/bin")
        }
        child.environment = environment
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = FileHandle.nullDevice
        do {
            try child.run()
            process = child
            input = stdinPipe.fileHandleForWriting
            output = stdoutPipe.fileHandleForReading
            currentState = .connected
            do {
                let _: [RunDTO] = try send(.listRuns, as: [RunDTO].self)
            } catch {
                stop()
                currentState = .error("Backend could not open the database")
                throw ControlClientError.transport("Backend could not start or open its database. Check Application Support permissions and use Reconnect Backend.")
            }
        } catch {
            if let error = error as? ControlClientError { throw error }
            currentState = .error("Could not start backend")
            throw ControlClientError.transport("Could not start the Python backend. Check the installed runtime and use Reconnect Backend.")
        }
    }

    public func shutdown() {
        stop()
    }

    public func send<T: Decodable & Sendable>(_ operation: ControlOperation, as type: T.Type) throws -> T {
        guard let process, process.isRunning, let input, let output else {
            currentState = .disconnected
            throw ControlClientError.unavailable
        }
        let requestID = UUID().uuidString
        do {
            try input.write(contentsOf: operation.encodedLine(id: requestID))
            guard let line = try readLine(from: output) else {
                currentState = .disconnected
                throw ControlClientError.unavailable
            }
            let envelope = try JSONDecoder.controlPlane.decode(ResponseEnvelope<T>.self, from: line)
            guard envelope.id == requestID else { throw ControlClientError.protocolMismatch }
            if let error = envelope.error { throw ControlClientError.backend(error) }
            guard envelope.ok, let result = envelope.result else { throw ControlClientError.protocolMismatch }
            return result
        } catch let error as ControlClientError {
            if case .backend = error { throw error }
            currentState = .disconnected
            throw error
        } catch {
            currentState = .disconnected
            throw ControlClientError.transport("Backend connection failed. Use Reconnect Backend to try again.")
        }
    }

    private func readLine(from handle: FileHandle) throws -> Data? {
        var line = Data()
        while line.count <= 1_048_576 {
            guard let byte = try handle.read(upToCount: 1), !byte.isEmpty else {
                return nil
            }
            if byte[0] == 0x0A { return line }
            line.append(byte)
        }
        throw ControlClientError.protocolMismatch
    }

    private func stop() {
        try? input?.close()
        if let process, process.isRunning {
            // Give the NDJSON service a bounded chance to finish after stdin EOF.
            for _ in 0..<20 where process.isRunning {
                Thread.sleep(forTimeInterval: 0.05)
            }
            if process.isRunning { process.terminate() }
            process.waitUntilExit()
        }
        try? output?.close()
        input = nil
        output = nil
        process = nil
        currentState = .disconnected
    }
}
