// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AgentControl",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "AgentControl", targets: ["AgentControl"]),
    ],
    dependencies: [.package(path: "../KeyboardEngine")],
    targets: [
        // Pure logic of the MCP agent control: protocol, tool schemas, key combos, coordinates,
        // the permission gate. No AppKit, no FreeRDP; the app glue lives in Sources/WeitblickRemote/Agent.
        .target(name: "AgentControl", dependencies: [.product(name: "KeyboardEngine", package: "KeyboardEngine")]),
        .testTarget(
            name: "AgentControlTests",
            dependencies: ["AgentControl", .product(name: "KeyboardEngine", package: "KeyboardEngine")]
        ),
    ]
)
