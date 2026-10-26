import ServiceManagement
import SwiftUI

/// A menu bar app with no Dock icon. It keeps the widget fresh and owns the countdown windows (D071).
@main
struct ArgusBarApp: App {
    @State private var model = BarModel()

    var body: some Scene {
        MenuBarExtra {
            BarMenu(model: model)
        } label: {
            Image(systemName: model.counting.isEmpty ? "checklist" : "timer")
                .accessibilityLabel("Argus")
        }
    }
}

private struct BarMenu: View {
    var model: BarModel
    @State private var atLogin = SMAppService.mainApp.status == .enabled

    var body: some View {
        Text(model.summary)
        if let asOf = model.asOf {
            Text("As of \(DueText.clock(asOf))")
        }
        Divider()
        Button("Open Argus") { NSWorkspace.shared.open(Argus.link("/today")) }
            .keyboardShortcut("o")
        Button("Refresh") { Task { await model.refresh(force: true) } }
            .keyboardShortcut("r")
        Toggle("Launch at Login", isOn: $atLogin)
            .onChange(of: atLogin) { _, on in
                do {
                    if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                } catch {
                    atLogin = SMAppService.mainApp.status == .enabled
                }
            }
        #if DEBUG
            Button("Show Test Countdown") { model.showTestCountdown() }
        #endif
        Divider()
        Button("Quit Argus") { NSApplication.shared.terminate(nil) }
            .keyboardShortcut("q")
    }
}
