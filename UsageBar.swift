// Zero-dependency macOS menubar app. Runs usage.py every 5 minutes and renders its JSON:
// menubar = vendor mark + the fullest window per vendor, menu = where to switch next + progress bar per gauge.
// Build: swiftc -O -o UsageBar UsageBar.swift   (see install.sh)
import Cocoa

struct Gauge: Decodable {
    let id: String; let short: String; let label: String; let percent: Int; let reset: String?; let group: String
    let fetchedAt: String?; let stale: Bool?
    var isStale: Bool { stale ?? false }
    var window: String { label.contains("·") ? String(label.split(separator: "·").last!).trimmingCharacters(in: .whitespaces) : label }
}
struct Payload: Decodable { let gauges: [Gauge]; let errors: [String] }

let scriptPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent().appendingPathComponent("usage.py").path
let groupOrder = ["claude", "gemini", "codex"]
let refreshSeconds: TimeInterval = 300
let groupTitles = ["claude": "CLAUDE", "gemini": "GEMINI (Antigravity)", "codex": "CODEX"]
let groupNames = ["claude": "Claude", "gemini": "Gemini", "codex": "Codex"]

// The gauge that limits work in a vendor right now: the fullest window. For Antigravity only the
// Gemini group counts (the Claude/GPT-through-Antigravity quota is a separate pool).
func limiting(_ gauges: [Gauge], _ group: String) -> Gauge? {
    gauges.filter { $0.group == group && ($0.group != "gemini" || $0.id.hasPrefix("antigravity:gemini")) }
          .max { $0.percent < $1.percent }
}

func color(_ p: Int) -> NSColor {
    p >= 80 ? .systemRed : p >= 50 ? .systemOrange : .systemGreen
}


// Vendor marks: the official logo shapes from icons/<group>.svg (Simple Icons, CC0 collection;
// the marks themselves stay their owners' trademarks), tinted per vendor. If a file is missing
// the in-house vector rendition below is drawn instead, so the app still needs no assets.
let iconDir = URL(fileURLWithPath: scriptPath).deletingLastPathComponent().appendingPathComponent("icons")
var iconCache: [String: NSImage?] = [:]
func brandIcon(_ group: String) -> NSImage? {
    if let cached = iconCache[group] { return cached }
    let img = NSImage(contentsOf: iconDir.appendingPathComponent("\(group).svg"))
    iconCache[group] = img
    return img
}

func vendorColor(_ group: String) -> NSColor {
    switch group {
    case "claude": return NSColor(srgbRed: 0.80, green: 0.47, blue: 0.36, alpha: 1)   // Anthropic clay
    case "gemini": return NSColor(srgbRed: 0.34, green: 0.55, blue: 0.96, alpha: 1)   // Google blue
    default: return NSColor(srgbRed: 0.45, green: 0.72, blue: 0.63, alpha: 1)         // OpenAI teal
    }
}

func vendorGlyph(_ group: String, size: CGFloat, tint: NSColor? = nil) -> NSImage {
    if let base = brandIcon(group) {
        return NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
            base.draw(in: rect)
            (tint ?? vendorColor(group)).set()
            rect.fill(using: .sourceAtop)
            return true
        }
    }
    return NSImage(size: NSSize(width: size, height: size), flipped: false) { rect in
        let c = NSPoint(x: rect.midX, y: rect.midY)
        let r = rect.width / 2 - 0.5
        (tint ?? vendorColor(group)).setStroke()
        (tint ?? vendorColor(group)).setFill()
        switch group {
        case "claude":                                    // radiating burst
            let spokes = 10
            let path = NSBezierPath()
            for i in 0..<spokes {
                let a = CGFloat(i) * .pi * 2 / CGFloat(spokes)
                path.move(to: NSPoint(x: c.x + cos(a) * r * 0.30, y: c.y + sin(a) * r * 0.30))
                path.line(to: NSPoint(x: c.x + cos(a) * r * 0.98, y: c.y + sin(a) * r * 0.98))
            }
            path.lineWidth = max(1, size * 0.11); path.lineCapStyle = .round; path.stroke()
        case "gemini":                                    // four-pointed sparkle
            let path = NSBezierPath()
            let pts = [NSPoint(x: c.x, y: c.y + r), NSPoint(x: c.x + r, y: c.y),
                       NSPoint(x: c.x, y: c.y - r), NSPoint(x: c.x - r, y: c.y)]
            path.move(to: pts[0])
            for i in 0..<4 {  // control points pulled off-centre so the arms read at 12 px
                let next = pts[(i + 1) % 4]
                let c1 = NSPoint(x: c.x + (pts[i].x - c.x) * 0.28, y: c.y + (pts[i].y - c.y) * 0.28)
                let c2 = NSPoint(x: c.x + (next.x - c.x) * 0.28, y: c.y + (next.y - c.y) * 0.28)
                path.curve(to: next, controlPoint1: c1, controlPoint2: c2)
            }
            path.close(); path.fill()
        default:                                          // interlocking rosette
            let path = NSBezierPath()
            for i in 0..<3 {
                let oval = NSBezierPath(ovalIn: NSRect(x: c.x - r * 0.42, y: c.y - r,
                                                       width: r * 0.84, height: r * 2))
                let t = NSAffineTransform()
                t.translateX(by: c.x, yBy: c.y)
                t.rotate(byDegrees: CGFloat(i) * 60)
                t.translateX(by: -c.x, yBy: -c.y)
                oval.transform(using: t as AffineTransform)
                path.append(oval)
            }
            path.lineWidth = max(0.9, size * 0.075); path.stroke()
        }
        return true
    }
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

func parseISO(_ iso: String?) -> Date? {
    guard let iso = iso else { return nil }
    let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.date(from: iso) ?? ISO8601DateFormatter().date(from: iso)
}

func resetText(_ iso: String?) -> String {
    guard let d = parseISO(iso) else { return "" }
    let mins = max(0, Int(d.timeIntervalSinceNow / 60))
    if mins >= 2880 { return "reset o \(mins / 1440) d" }
    return String(format: "reset o %dh%02d", mins / 60, mins % 60)
}

func agoText(_ iso: String?) -> String {
    guard let d = parseISO(iso) else { return "" }
    let mins = max(0, Int(-d.timeIntervalSinceNow / 60))
    if mins < 1 { return "práve teraz" }
    if mins < 60 { return "pred \(mins) min" }
    if mins < 2880 { return "pred \(mins / 60) h" }
    return "pred \(mins / 1440) d"
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

final class SectionHeader: NSView {
    init(title: String, group: String?, status: String = "") {
        super.init(frame: NSRect(x: 0, y: 0, width: 320, height: 26))
        if let group = group {
            let icon = NSImageView(frame: NSRect(x: 18, y: 6, width: 13, height: 13))
            icon.image = vendorGlyph(group, size: 13); addSubview(icon)
        }
        let label = NSTextField(labelWithString: title.uppercased())
        label.frame = NSRect(x: group == nil ? 18 : 37, y: 5, width: 180, height: 15)
        label.font = .systemFont(ofSize: 10, weight: .semibold)
        label.textColor = group.map(vendorColor) ?? .secondaryLabelColor
        let right = NSTextField(labelWithString: status)
        right.frame = NSRect(x: 160, y: 5, width: 142, height: 15); right.alignment = .right
        right.font = .systemFont(ofSize: 10); right.textColor = .systemOrange
        [label, right].forEach(addSubview)
    }
    required init?(coder: NSCoder) { nil }
}

// One line per vendor, most headroom first: which tool to hand the next task to.
final class SwitchRow: NSView {
    init(rank: Int, group: String, g: Gauge?) {
        super.init(frame: NSRect(x: 0, y: 0, width: 320, height: 34))
        let icon = NSImageView(frame: NSRect(x: 18, y: 12, width: 13, height: 13))
        icon.image = vendorGlyph(group, size: 13, tint: g == nil ? .tertiaryLabelColor : nil)
        let name = NSTextField(labelWithString: "\(rank). \(groupNames[group] ?? group)")
        name.frame = NSRect(x: 37, y: 15, width: 150, height: 16); name.font = .systemFont(ofSize: 12, weight: rank == 1 ? .semibold : .regular)
        let free = NSTextField(labelWithString: g.map { "\(100 - $0.percent) % voľné" } ?? "bez dát")
        free.frame = NSRect(x: 190, y: 15, width: 112, height: 16); free.alignment = .right
        free.font = .monospacedDigitSystemFont(ofSize: 12, weight: .semibold)
        free.textColor = g.map { color($0.percent) } ?? .tertiaryLabelColor
        let sub = NSTextField(labelWithString: g.map { "limituje \($0.window) · \(resetText($0.reset))" } ?? "zdroj zlyhal, pozri nižšie")
        sub.frame = NSRect(x: 37, y: 2, width: 265, height: 13); sub.font = .systemFont(ofSize: 10); sub.textColor = .tertiaryLabelColor
        if g == nil { name.textColor = .tertiaryLabelColor }
        [icon, name, free, sub].forEach(addSubview)
    }
    required init?(coder: NSCoder) { nil }
}

final class BarRow: NSView {
    init(_ g: Gauge) {
        super.init(frame: NSRect(x: 0, y: 0, width: 320, height: 42))
        let name = g.label.hasPrefix("Gemini · ") || g.label.hasPrefix("Claude · ") || g.label.hasPrefix("Codex · ") ? g.window : g.label
        let label = NSTextField(labelWithString: name)
        label.frame = NSRect(x: 37, y: 22, width: 190, height: 16); label.font = .systemFont(ofSize: 12)
        let pct = NSTextField(labelWithString: "\(g.percent) %")
        pct.frame = NSRect(x: 230, y: 22, width: 72, height: 16); pct.alignment = .right
        pct.font = .monospacedDigitSystemFont(ofSize: 12, weight: .semibold); pct.textColor = color(g.percent)
        let reset = NSTextField(labelWithString: resetText(g.reset))
        reset.frame = NSRect(x: 37, y: 4, width: 265, height: 13)
        reset.font = .systemFont(ofSize: 10); reset.textColor = .tertiaryLabelColor
        let bar = Bar(frame: NSRect(x: 37, y: 17, width: 265, height: 4), percent: g.percent)
        if g.isStale { label.textColor = .tertiaryLabelColor; pct.textColor = .tertiaryLabelColor; bar.alphaValue = 0.35 }
        [label, pct, reset, bar].forEach(addSubview)
    }
    required init?(coder: NSCoder) { nil }
}

final class App: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    let menu = NSMenu()
    var refreshing = false
    var refreshItem: NSMenuItem?

    func applicationDidFinishLaunching(_ note: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "…"
        item.menu = menu
        refresh()
        Timer.scheduledTimer(withTimeInterval: refreshSeconds, repeats: true) { _ in self.refresh() }
    }

    func refresh() {
        guard !refreshing else { return }  // one collector at a time: no duplicate agy processes, no older result overwriting a newer one
        refreshing = true
        refreshItem?.title = "Obnovujem…"; refreshItem?.isEnabled = false
        DispatchQueue.global().async {
            let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/python3"); p.arguments = [scriptPath]
            let out = Pipe(), err = Pipe(); p.standardOutput = out; p.standardError = err
            do { try p.run() } catch { DispatchQueue.main.async { self.refreshing = false; self.render(nil, ["\(error)"]) }; return }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let stderr = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit()
            var payload: Payload?, errors: [String] = []
            let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
            do { payload = try decoder.decode(Payload.self, from: data) } catch { errors.append("JSON: \(String(describing: error).prefix(120))") }
            if p.terminationStatus != 0 { errors.append("usage.py skončil s kódom \(p.terminationStatus)") }
            if payload == nil && !stderr.isEmpty { errors.append(String(stderr.trimmingCharacters(in: .whitespacesAndNewlines).suffix(200))) }
            DispatchQueue.main.async { self.refreshing = false; self.render(payload, errors) }
        }
    }

    func render(_ payload: Payload?, _ extraErrors: [String]) {
        let gauges = payload?.gauges ?? []
        let ranked = groupOrder.map { ($0, limiting(gauges, $0)) }
            .sorted { a, b in (a.1.map { $0.isStale ? -1 : 100 - $0.percent } ?? -2) > (b.1.map { $0.isStale ? -1 : 100 - $0.percent } ?? -2) }

        // Menubar: per vendor the limiting window, "7d" flagged when it is the weekly one.
        let title = NSMutableAttributedString()
        let numberFont = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        let smallFont = NSFont.monospacedDigitSystemFont(ofSize: 9, weight: .regular)
        for group in groupOrder {
            guard let g = limiting(gauges, group) else { continue }
            let mark = NSTextAttachment(); mark.image = vendorGlyph(group, size: 12, tint: g.isStale ? .tertiaryLabelColor : nil)
            mark.bounds = NSRect(x: 0, y: -2, width: 12, height: 12)
            title.append(NSAttributedString(attachment: mark))
            let tint: NSColor = g.isStale ? .tertiaryLabelColor : color(g.percent)
            title.append(NSAttributedString(string: " \(g.percent)%", attributes: [.font: numberFont, .foregroundColor: tint]))
            if g.window.hasPrefix("týždeň") { title.append(NSAttributedString(string: " 7d", attributes: [.font: smallFont, .foregroundColor: tint, .baselineOffset: 1])) }
            title.append(NSAttributedString(string: "   ", attributes: [.font: numberFont]))
        }
        item.button?.attributedTitle = title.length > 0 ? title : NSAttributedString(string: "usage ?")

        menu.removeAllItems()
        if ranked.contains(where: { $0.1 != nil }) {
            let h = NSMenuItem(); h.isEnabled = false; h.view = SectionHeader(title: "Kam prepnúť teraz", group: nil); menu.addItem(h)
            for (i, (group, g)) in ranked.enumerated() {
                let mi = NSMenuItem(); mi.view = SwitchRow(rank: i + 1, group: group, g: g.flatMap { $0.isStale ? nil : $0 }); menu.addItem(mi)
            }
            menu.addItem(.separator())
        }
        var lastGroup = ""
        for g in gauges {
            if g.group != lastGroup {
                if !lastGroup.isEmpty { menu.addItem(.separator()) }
                let h = NSMenuItem(); h.isEnabled = false
                let status = g.isStale ? "zastarané · \(agoText(g.fetchedAt))" : ""
                h.view = SectionHeader(title: groupTitles[g.group] ?? g.group.uppercased(), group: g.group, status: status)
                menu.addItem(h); lastGroup = g.group
            }
            let mi = NSMenuItem(); mi.view = BarRow(g); menu.addItem(mi)
        }
        for e in (payload?.errors ?? []) + extraErrors where !e.isEmpty {
            menu.addItem(.separator()); let mi = NSMenuItem(title: "⚠ \(e)", action: nil, keyEquivalent: ""); mi.isEnabled = false; menu.addItem(mi)
        }
        menu.addItem(.separator())
        let freshest = gauges.filter { !$0.isStale }.compactMap { parseISO($0.fetchedAt) }.max()
        let f = DateFormatter(); f.dateFormat = "HH:mm"
        let stampText = freshest.map { "Aktualizované \(f.string(from: $0))" } ?? (gauges.isEmpty ? "Bez dát" : "Bez čerstvých dát")
        let stamp = NSMenuItem(title: stampText, action: nil, keyEquivalent: ""); stamp.isEnabled = false; menu.addItem(stamp)
        let r = NSMenuItem(title: refreshing ? "Obnovujem…" : "Obnoviť", action: #selector(doRefresh), keyEquivalent: "r")
        r.target = self; r.isEnabled = !refreshing; refreshItem = r; menu.addItem(r)
        menu.addItem(NSMenuItem(title: "Ukončiť", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
    }

    @objc func doRefresh() { refresh() }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = App()
app.delegate = delegate
app.run()
