// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "JobAgentControl",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "JobAgentControl", targets: ["JobAgentControl"]),
        .library(name: "JobAgentControlCore", targets: ["JobAgentControlCore"]),
    ],
    targets: [
        .target(name: "JobAgentControlCore"),
        .executableTarget(name: "JobAgentControl", dependencies: ["JobAgentControlCore"]),
        .testTarget(name: "JobAgentControlCoreTests", dependencies: ["JobAgentControlCore"],
                    path: "macos/Tests/JobAgentControlCoreTests"),
    ]
)
