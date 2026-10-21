import ConnectionStore
import SwiftUI

/// What the overview's list, menus and buttons can do (implemented by the overview window).
struct OverviewActions {
    var connect: (Set<UUID>) -> Void
    var edit: (UUID) -> Void
    var duplicate: (UUID) -> Void
    var export: (UUID) -> Void
    var wake: (UUID) -> Void
    var delete: (Set<UUID>) -> Void
    var newConnection: () -> Void
    var importFromJump: () -> Void
    var jumpAvailable: Bool
}

/// The overview tab: all saved connections.
struct OverviewView: View {
    @Bindable var model: OverviewModel
    let actions: OverviewActions

    var body: some View {
        content.frame(minWidth: 480, minHeight: 320)
    }

    @ViewBuilder private var content: some View {
        if let error = model.library.loadError {
            ContentUnavailableView {
                Label("Can’t Read Connections", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            }
        } else if model.library.connections.isEmpty {
            ContentUnavailableView {
                Label("No Connections Yet", systemImage: "pc")
            } description: {
                Text("Create a connection or import your connections from Jump Desktop.")
            } actions: {
                HStack {
                    Button("New Connection…", action: actions.newConnection)
                    if actions.jumpAvailable { Button("Import from Jump Desktop…", action: actions.importFromJump) }
                }
            }
        } else if model.visibleConnections.isEmpty {
            ContentUnavailableView.search(text: model.searchText)
        } else {
            list
        }
    }

    private var list: some View {
        List(selection: $model.selection) {
            ForEach(model.visibleConnections) { connection in
                ConnectionRow(connection: connection, status: model.activity.statuses[connection.id],
                              agent: model.activity.agents[connection.id], watch: { actions.connect([connection.id]) })
                    .tag(connection.id)
            }
        }
        .listStyle(.inset(alternatesRowBackgrounds: false))
        .contextMenu(forSelectionType: UUID.self) { ids in
            menu(for: ids)
        } primaryAction: { ids in
            actions.connect(ids)
        }
        // Only rows the search shows: a hidden selected row must not be deleted or opened.
        .onDeleteCommand { actions.delete(visibleSelection) }
        .onKeyPress(.return) {
            guard !visibleSelection.isEmpty else { return .ignored }
            actions.connect(visibleSelection)
            return .handled
        }
    }

    private var visibleSelection: Set<UUID> { Set(model.selectedConnections.map(\.id)) }

    @ViewBuilder private func menu(for ids: Set<UUID>) -> some View {
        if !ids.isEmpty {
            if ids.count == 1 {
                Button("Connect") { actions.connect(ids) }
            } else {
                Button("Open \(ids.count) Connections") { actions.connect(ids) }
            }
            if ids.count == 1, let id = ids.first {
                Divider()
                Button("Edit…") { actions.edit(id) }
                Button("Duplicate") { actions.duplicate(id) }
                if model.library.connection(with: id)?.protocol == .rdp {
                    Button("Export as .rdp File…") { actions.export(id) }
                }
                if model.library.connection(with: id)?.advanced.wakeOnLANMACAddresses.isEmpty == false {
                    Button("Wake Up (Wake-on-LAN)") { actions.wake(id) }
                }
            }
            Divider()
            if ids.count == 1 {
                Button("Delete…", role: .destructive) { actions.delete(ids) }
            } else {
                Button("Delete \(ids.count) Connections…", role: .destructive) { actions.delete(ids) }
            }
        } else {
            Button("New Connection…", action: actions.newConnection)
        }
    }
}
