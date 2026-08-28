// Zero-dependency macOS menubar app. Runs usage.py every 5 minutes and renders its JSON:
// menubar = letter + colored ring per headline gauge, menu = progress bar per gauge.
// Build: swiftc -O -o UsageBar UsageBar.swift   (see install.sh)
import Cocoa

struct Gauge: Decodable { let id: String; let short: String; let label: String; let percent: Int; let reset: String?; let group: String }
struct Payload: Decodable { let gauges: [Gauge]; let errors: [String] }

let scriptPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().appendingPathComponent("usage.py").path
let headline = ["claude:session", "claude:weekly_scoped", "gemini:gemini-3.7-flash-tiered", "codex:primary_window"]
let refreshSeconds: TimeInterval = 300
let groupTitles = ["claude": "CLAUDE", "gemini": "GEMINI (Antigravity)", "codex": "CODEX"]

func color(_ p: Int) -> NSColor {
    p >= 80 ? .systemRed : p >= 50 ? .systemOrange : .systemGreen
}

func ring(_ p: Int, size: CGFloat = 14) -> NSImage {
    let img = NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
        let c = NSPoint(x: rect.midX, y: rect.midY), r = rect.width / 2 - 1.5
        let track = NSBezierPath(ovalIn: rect.insetBy(dx: 1.5, dy: 1.5))
        track.lineWidth = 2.5; NSColor.tertiaryLabelColor.setStroke(); track.stroke()
        let arc = NSBezierPath()
        arc.appendArc(withCenter: c, radius: r, startAngle: 90, endAngle: 90 - 360 * CGFloat(max(p, 2)) / 100, clockwise: true)
        arc.lineWidth = 2.5; arc.lineCapStyle = .round; color(p).setStroke(); arc.stroke()
        return true
    }
    img.isTemplate = false
    return img
}

func resetText(_ iso: String?) -> String {
    guard let iso = iso else { return "" }
    let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let g = ISO8601DateFormatter()
    guard let d = f.date(from: iso) ?? g.date(from: iso) else { return "" }
    let mins = max(0, Int(d.timeIntervalSinceNow / 60))
    if mins >= 2880 { return "reset o \(mins / 1440) d" }
    return String(format: "reset o %dh%02d", mins / 60, mins % 60)
}

final class Bar: NSView {
    let percent: Int
    init(frame: NSRect, percent: Int) { self.percent = percent; super.init(frame: frame) }
    required init?(coder: NSCoder) { nil }
    override func draw(_ rect: NSRect) {
        NSColor.quaternaryLabelColor.setFill(); NSBezierPath(roundedRect: bounds, xRadius: 2.5, yRadius: 2.5).fill()
        var f = bounds; f.size.width = max(4, bounds.width * CGFloat(percent) / 100)
        color(percent).setFill(); NSBezierPath(roundedRect: f, xRadius: 2.5, yRadius: 2.5).fill()
    }
}

final class BarRow: NSView {
    init(_ g: Gauge) {
        super.init(frame: NSRect(x: 0, y: 0, width: 300, height: 40))
        let label = NSTextField(labelWithString: g.label)
        label.frame = NSRect(x: 14, y: 21, width: 190, height: 16); label.font = .systemFont(ofSize: 12)
        let pct = NSTextField(labelWithString: "\(g.percent) %")
        pct.frame = NSRect(x: 210, y: 21, width: 76, height: 16); pct.alignment = .right
        pct.font = .monospacedDigitSystemFont(ofSize: 12, weight: .semibold); pct.textColor = color(g.percent)
        let reset = NSTextField(labelWithString: resetText(g.reset))
        reset.frame = NSRect(x: 14, y: 3, width: 272, height: 13); reset.font = .systemFont(ofSize: 10); reset.textColor = .secondaryLabelColor
        let bar = Bar(frame: NSRect(x: 14, y: 16, width: 272, height: 5), percent: g.percent)
        [label, pct, reset, bar].forEach(addSubview)
    }
    required init?(coder: NSCoder) { nil }
}

final class App: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    let menu = NSMenu()

    func applicationDidFinishLaunching(_ note: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "…"
        item.menu = menu
        refresh()
        Timer.scheduledTimer(withTimeInterval: refreshSeconds, repeats: true) { _ in self.refresh() }
    }

    func refresh() {
        DispatchQueue.global().async {
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/python3"); p.arguments = [scriptPath]
            let out = Pipe(), err = Pipe(); p.standardOutput = out; p.standardError = err
            do { try p.run() } catch { DispatchQueue.main.async { self.render(nil, ["\(error)"]) }; return }
            p.waitUntilExit()
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let payload = try? JSONDecoder().decode(Payload.self, from: data)
            let stderr = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async { self.render(payload, payload == nil ? [String(stderr.suffix(200))] : []) }
        }
    }

    func render(_ payload: Payload?, _ extraErrors: [String]) {
        let gauges = payload?.gauges ?? []
        let title = NSMutableAttributedString()
        let letterFont = NSFont.systemFont(ofSize: 10, weight: .semibold)
        let numberFont = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        for id in headline {
            guard let g = gauges.first(where: { $0.id == id }) else { continue }
            title.append(NSAttributedString(string: "\(g.short) ", attributes: [.font: letterFont, .foregroundColor: NSColor.secondaryLabelColor]))
            let att = NSTextAttachment(); att.image = ring(g.percent); att.bounds = NSRect(x: 0, y: -3, width: 14, height: 14)
            title.append(NSAttributedString(attachment: att))
            title.append(NSAttributedString(string: " \(g.percent)%   ", attributes: [.font: numberFont]))
        }
        item.button?.attributedTitle = title.length > 0 ? title : NSAttributedString(string: "usage ?")

        menu.removeAllItems()
        var lastGroup = ""
        for g in gauges {
            if g.group != lastGroup {
                if !lastGroup.isEmpty { menu.addItem(.separator()) }
                let h = NSMenuItem(title: groupTitles[g.group] ?? g.group.uppercased(), action: nil, keyEquivalent: ""); h.isEnabled = false
                menu.addItem(h); lastGroup = g.group
            }
            let mi = NSMenuItem(); mi.view = BarRow(g); menu.addItem(mi)
        }
        for e in (payload?.errors ?? []) + extraErrors where !e.isEmpty {
            menu.addItem(.separator()); let mi = NSMenuItem(title: "⚠ \(e)", action: nil, keyEquivalent: ""); mi.isEnabled = false; menu.addItem(mi)
        }
        menu.addItem(.separator())
        let f = DateFormatter(); f.dateFormat = "HH:mm"
        let stamp = NSMenuItem(title: "Aktualizované \(f.string(from: Date()))", action: nil, keyEquivalent: ""); stamp.isEnabled = false; menu.addItem(stamp)
        let r = NSMenuItem(title: "Obnoviť", action: #selector(doRefresh), keyEquivalent: "r"); r.target = self; menu.addItem(r)
        menu.addItem(NSMenuItem(title: "Ukončiť", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    @objc func doRefresh() { refresh() }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = App()
app.delegate = delegate
app.run()
