import ConnectionStore
import SwiftUI

struct EditorSecurityPage: View {
    @Binding var security: SecuritySettings

    var body: some View {
        Form {
            Section {
                Toggle(isOn: Binding(get: { !security.disableNLA }, set: { security.disableNLA = !$0 })) {
                    Text("Network Level Authentication (NLA)")
                    Text("Signs in before the connection is set up. Turn off only for very old Windows versions.")
                }
                Toggle(isOn: $security.ignoreCertificateErrors) {
                    Text("Don’t check the certificate")
                    Text("Never asks about the certificate. Only on trusted networks.")
                }
            }
            Section {
                if security.trustedCertificateFingerprints.isEmpty {
                    Text("None yet. You’ll be asked on the first connection.").foregroundStyle(.secondary)
                }
                ForEach(security.trustedCertificateFingerprints, id: \.self) { fingerprint in
                    HStack {
                        Text(fingerprint).font(.caption.monospaced()).textSelection(.enabled).lineLimit(2)
                        Spacer()
                        Button {
                            security.trustedCertificateFingerprints.removeAll { $0 == fingerprint }
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(.borderless)
                        .help("Stop Trusting")
                    }
                }
            } header: {
                Text("Trusted certificates (SHA-256)")
            }
            Section {
                Picker("AI agent access", selection: $security.agentAccess) {
                    Text("Ask").tag(AgentAccess.ask)
                    Text("Always allow").tag(AgentAccess.always)
                    Text("Never").tag(AgentAccess.never)
                }
            } footer: {
                Text("Whether AI agents may control this connection over MCP (Settings → AI agents). “Ask” shows a prompt each time an agent wants it.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}
