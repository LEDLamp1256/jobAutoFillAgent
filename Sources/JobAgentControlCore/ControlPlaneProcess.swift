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
        let child = Process()
        child.executableURL = URL(fileURLWithPath: configuration.pythonExecutable)
        child.arguments = ["-m", "jobagent.control_plane_stdio", "--db", configuration.databasePath]
        child.currentDirectoryURL = configuration.repositoryRoot
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = FileHandle.standardError
        do {
            try child.run()
            process = child
            input = stdinPipe.fileHandleForWriting
            output = stdoutPipe.fileHandleForReading
            currentState = .connected
        } catch {
            currentState = .error("Could not start backend")
            throw ControlClientError.transport("Could not start backend")
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
            throw ControlClientError.transport("Backend transport failed")
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
