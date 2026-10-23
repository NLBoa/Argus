import AppKit
import SwiftUI

/// The glass countdown windows, stacked at the top right below the menu bar. Each stays until it's
/// dismissed, the work leaves the feed (done), or its due time passes.
@MainActor
final class CountdownPanels {
    static let width: CGFloat = 360
    static let margin: CGFloat = 12
    private var open: [(id: Int, panel: NSPanel)] = []

    /// Test windows (negative ids, Debug only) aren't closed by `closeAll`.
    func show(_ item: FeedItem) {
        if let i = open.firstIndex(where: { $0.id == item.id }) {
            open[i].panel.orderFrontRegardless()  // a later milestone brings it back to the front
            return
        }
        let panel = GlassPanel()
        let view = CountdownView(
            item: item,
            open: { [weak self] in
                NSWorkspace.shared.open(Argus.link(item.url))
                self?.close(item.id)
            },
            dismiss: { [weak self] in self?.close(item.id) })
        let host = NSHostingController(rootView: view)
        let size = host.sizeThatFits(in: CGSize(width: Self.width + 2, height: 400))
        panel.contentViewController = host
        panel.setContentSize(size)
        open.append((item.id, panel))
        layout(animatingIn: panel)
    }

    func closeAll(except keep: Set<Int>) {
        for entry in open where entry.id >= 0 && !keep.contains(entry.id) {
            close(entry.id)
        }
    }

    private func close(_ id: Int) {
        guard let i = open.firstIndex(where: { $0.id == id }) else { return }
        let panel = open.remove(at: i).panel
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = 0.2
            panel.animator().alphaValue = 0
        } completionHandler: {
            Task { @MainActor in panel.orderOut(nil) }
        }
        layout(animatingIn: nil)
    }

    private func layout(animatingIn new: NSPanel?) {
        guard let screen = NSScreen.main?.visibleFrame else { return }
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        var top = screen.maxY - Self.margin
        for (_, panel) in open {
            let size = panel.frame.size
            let frame = NSRect(
                x: screen.maxX - size.width - Self.margin, y: top - size.height,
                width: size.width, height: size.height)
            top = frame.minY - Self.margin
            if panel === new {
                // Slide in from the right; with Reduce Motion, fade in place.
                panel.setFrame(reduceMotion ? frame : frame.offsetBy(dx: size.width / 2, dy: 0), display: false)
                panel.alphaValue = 0
                panel.orderFrontRegardless()
                NSAnimationContext.runAnimationGroup { ctx in
                    ctx.duration = reduceMotion ? 0.2 : 0.35
                    ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
                    panel.animator().setFrame(frame, display: true)
                    panel.animator().alphaValue = 1
                }
            } else {
                panel.animator().setFrame(frame, display: true)
            }
        }
    }
}

/// Borderless, transparent, and non-activating: it floats over every Space without taking focus.
private final class GlassPanel: NSPanel {
    init() {
        super.init(
            contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered,
            defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        hidesOnDeactivate = false
        isOpaque = false
        backgroundColor = .clear
        hasShadow = false  // the glass draws its own
        isMovableByWindowBackground = true
        isReleasedWhenClosed = false
    }

    override var canBecomeKey: Bool { true }
}

private struct CountdownView: View {
    var item: FeedItem
    var open: () -> Void
    var dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 12) {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(Palette.accent(item.accent))
                    .frame(width: 3, height: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.title)
                        .font(.headline)
                        .lineLimit(2)
                    Text("\(item.course) · due \(DueText.clock(item.dueAt))")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 8)
                Text(timerInterval: Date.now...max(item.dueAt, .now), countsDown: true)
                    .font(.system(size: 28, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(Palette.brand)
                    .accessibilityLabel(Text("Time left"))
                    .accessibilityValue(Text(timerInterval: Date.now...max(item.dueAt, .now), countsDown: true))
            }
            HStack(spacing: 8) {
                Spacer()
                Button("Dismiss", action: dismiss)
                    .buttonStyle(.glass)
                    .keyboardShortcut(.cancelAction)
                Button("Open in Argus", action: open)
                    .buttonStyle(.glassProminent)
                    .tint(Palette.brand)
            }
            .controlSize(.regular)
        }
        .padding(16)
        .frame(width: CountdownPanels.width)
        .glassEffect(.regular, in: .rect(cornerRadius: 20))
        .padding(1)  // keep the glass edge inside the window
    }
}
