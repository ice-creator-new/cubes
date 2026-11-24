//
//  DesignSystem.swift
//  cubes
//
//  Adaptive cool-neutral surfaces, semantic status colour and the small pieces shared by the
//  team screens. Blobatar faces carry identity, so the chrome stays quiet; long-form text sits
//  on opaque surfaces (the installed SwiftPieces GlassSurface is reserved for compact controls).
//

import SwiftUI
import UIKit

// MARK: - Colour plumbing

private extension UIColor {
    convenience init(rgb: UInt32) {
        self.init(
            red: CGFloat((rgb >> 16) & 0xFF) / 255.0,
            green: CGFloat((rgb >> 8) & 0xFF) / 255.0,
            blue: CGFloat(rgb & 0xFF) / 255.0,
            alpha: 1
        )
    }
}

private extension Color {
    /// A colour that resolves per appearance without Asset Catalog entries.
    init(light: UInt32, dark: UInt32) {
        self.init(uiColor: UIColor { traits in
            traits.userInterfaceStyle == .dark ? UIColor(rgb: dark) : UIColor(rgb: light)
        })
    }
}

// MARK: - Palette

enum Palette {
    /// Neutral chrome. Identity lives in the avatars; status colour is information, never decoration.
    static let canvas = Color(light: 0xF5F6F8, dark: 0x0B0D12)
    static let surface = Color(light: 0xFFFFFF, dark: 0x161A21)
    static let surfaceMuted = Color(light: 0xE9ECF1, dark: 0x22262F)
    static let ink = Color(light: 0x14161C, dark: 0xF2F4F8)
    static let inkSecondary = Color(light: 0x4F5766, dark: 0xB4BBC8)
    static let inkTertiary = Color(light: 0x6B7382, dark: 0x9098A6)
    static let hairline = Color(light: 0xDDE1E8, dark: 0x30353F)

    /// The one accent: the user's send action and anything that is currently working.
    static let accent = Color(light: 0x2B62E0, dark: 0x7FA3FF)
    static let onAccent = Color(light: 0xFFFFFF, dark: 0x0B0D12)

    static let completed = Color(light: 0x2E7D57, dark: 0x6FD39C)
    static let stopped = Color(light: 0xA26A15, dark: 0xE2B15F)
    static let failed = Color(light: 0xB4373F, dark: 0xF08A8E)

    static func status(_ status: RunStatus) -> Color {
        switch status {
        case .running: return accent
        case .completed: return completed
        case .stopped: return stopped
        case .error: return failed
        case .limited: return stopped
        }
    }

    static func status(_ status: ActionStatus) -> Color {
        switch status {
        case .running: return accent
        case .completed: return completed
        case .error: return failed
        }
    }
}

// MARK: - Type scale

enum AppFont {
    static let title = Font.system(.title3, weight: .semibold)
    static let section = Font.system(.headline, weight: .semibold)
    static let body = Font.system(.body)
    static let callout = Font.system(.callout)
    static let subheadline = Font.system(.subheadline)
    static let footnote = Font.system(.footnote)
    static let caption = Font.system(.caption)
    static let caption2 = Font.system(.caption2)
    static let mono = Font.system(.footnote, design: .monospaced)
}

// MARK: - Surfaces

/// Opaque card: the readable ground for long text, never glass.
private struct CardSurface: ViewModifier {
    let radius: CGFloat

    func body(content: Content) -> some View {
        let shape = RoundedRectangle(cornerRadius: radius, style: .continuous)
        content.background {
            shape
                .fill(Palette.surface)
                .overlay(shape.strokeBorder(Palette.hairline, lineWidth: 1))
        }
    }
}

extension View {
    func cardSurface(radius: CGFloat = 18) -> some View {
        modifier(CardSurface(radius: radius))
    }

    /// Readable measure for long text on wide screens.
    func readableWidth() -> some View {
        frame(maxWidth: 640)
            .frame(maxWidth: .infinity)
    }
}

// MARK: - Status pieces

struct ActionStatusPill: View {
    let status: ActionStatus

    var body: some View {
        let color = Palette.status(status)
        HStack(spacing: 4) {
            switch status {
            case .running:
                ProgressView()
                    .controlSize(.mini)
                    .tint(color)
            case .completed:
                Image(systemName: "checkmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(color)
            case .error:
                Image(systemName: "exclamationmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(color)
            }
            Text(status.displayName)
                .font(AppFont.caption2.weight(.medium))
                .foregroundStyle(Palette.inkSecondary)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(Capsule().fill(color.opacity(0.12)))
    }
}

/// Three dots that rise in sequence; under Reduce Motion they hold still and ripple opacity.
struct PulseDots: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let color: Color

    @State private var phase = 0

    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3, id: \.self) { index in
                Circle()
                    .fill(color)
                    .frame(width: 6, height: 6)
                    .opacity(reduceMotion ? (phase == index ? 0.9 : 0.4) : (phase == index ? 1 : 0.3))
                    .offset(y: reduceMotion ? 0 : (phase == index ? -3 : 0))
            }
        }
        .animation(.easeInOut(duration: 0.35), value: phase)
        .task {
            while !Task.isCancelled {
                for index in 0..<3 {
                    phase = index
                    try? await Task.sleep(for: .milliseconds(240))
                }
                try? await Task.sleep(for: .milliseconds(160))
            }
        }
        .accessibilityHidden(true)
    }
}

struct BreathingMark: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    let color: Color
    var size: CGFloat = 26

    @State private var expanded = false

    var body: some View {
        ZStack {
            Circle().fill(color.opacity(0.15))
            Circle().strokeBorder(color.opacity(0.3), lineWidth: 1)
            Image(systemName: "sparkle")
                .font(.system(size: size * 0.4, weight: .semibold))
                .foregroundStyle(color)
                .rotationEffect(.degrees(reduceMotion ? 0 : (expanded ? 12 : -12)))
        }
        .frame(width: size, height: size)
        .scaleEffect(reduceMotion ? 1 : (expanded ? 1.02 : 0.9))
        .animation(
            reduceMotion ? nil : .easeInOut(duration: 1.5).repeatForever(autoreverses: true),
            value: expanded
        )
        .onAppear {
            guard !reduceMotion else { return }
            expanded = true
        }
    }
}

/// A member's "working" placeholder: who is thinking, what about, and for how long.
struct ThinkingIndicator: View {
    var title: String = "主管正在工作"
    let step: String
    let startedAt: Date?

    var body: some View {
        Group {
            if let startedAt {
                TimelineView(.periodic(from: startedAt, by: 1)) { context in
                    pill(elapsed: ISODate.elapsed(since: startedAt, now: context.date))
                }
            } else {
                pill(elapsed: nil)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(step)
    }

    private func pill(elapsed: String?) -> some View {
        HStack(alignment: .center, spacing: 9) {
            BreathingMark(color: Palette.accent, size: 24)
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(AppFont.footnote.weight(.semibold))
                    .foregroundStyle(Palette.ink)
                Text(elapsed.map { "\(step) · \($0)" } ?? step)
                    .font(AppFont.caption)
                    .foregroundStyle(Palette.inkTertiary)
                    .lineLimit(2)
            }
            PulseDots(color: Palette.accent)
        }
        .padding(.leading, 5)
        .padding(.trailing, 12)
        .padding(.vertical, 5)
        .glassSurface(.capsule, style: .init(shadow: 0.05))
    }
}

// MARK: - Message body

/// Plain, high-contrast message text with an accent cursor while it streams.
struct StreamingBody: View {
    let text: String
    let tint: Color
    let isStreaming: Bool

    var body: some View {
        Text(attributed)
            .font(AppFont.body)
            .foregroundStyle(Palette.ink)
    }

    private var attributed: AttributedString {
        var string = AttributedString(text)
        guard isStreaming else { return string }
        var cursor = AttributedString("▌")
        cursor.foregroundColor = tint
        string.append(cursor)
        return string
    }
}

// MARK: - Shared states

/// One conversation's live task: who is answering, who is queued, and a stop control.
/// Several can exist at once, so callers pass the conversation title when they are not inside it.
struct RunBanner: View {
    let run: RunDTO
    let agents: [AgentDTO]
    /// `nil` when the task is running in the conversation being read.
    var conversationTitle: String?
    let isStopping: Bool
    let stopIdentifier: String
    var onOpen: (() -> Void)?
    let onStop: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            PulseDots(color: Palette.accent)

            VStack(alignment: .leading, spacing: 1) {
                Text(headline)
                    .font(AppFont.footnote.weight(.medium))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                if let progress = run.progressText {
                    Text(progress)
                        .font(AppFont.caption2)
                        .foregroundStyle(Palette.inkTertiary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 6)

            if let conversationTitle, let onOpen {
                Button("查看") { onOpen() }
                    .font(AppFont.caption.weight(.semibold))
                    .padding(.horizontal, 14)
                    .frame(minHeight: 44)
                    .glassSurface(.capsule, interactive: true, style: .init(shadow: 0.05))
                    .buttonStyle(.plain)
                    .accessibilityLabel("查看「\(conversationTitle)」")
                    .accessibilityIdentifier("viewRunConversation")
            }

            Button(action: onStop) {
                HStack(spacing: 5) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 10, weight: .bold))
                    Text("停止")
                        .font(AppFont.caption.weight(.semibold))
                }
                .padding(.horizontal, 14)
                .frame(minHeight: 44)
                .glassSurface(.capsule, interactive: true, style: .init(shadow: 0.05))
            }
            .buttonStyle(.plain)
            .disabled(isStopping)
            .accessibilityIdentifier(stopIdentifier)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .cardSurface(radius: 16)
        .accessibilityIdentifier("runBanner")
    }

    private var headline: String {
        let status = run.statusHeadline(agents: agents)
        guard let conversationTitle else { return status }
        return "「\(conversationTitle)」· \(status)"
    }
}

struct TeamErrorCard: View {
    let title: String
    let message: String
    var retryTitle: String = "重新加载"
    var retryIdentifier: String = "reloadConversation"
    let onRetry: () -> Void
    var onDismiss: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: "exclamationmark.triangle")
                .font(AppFont.footnote.weight(.semibold))
                .foregroundStyle(Palette.failed)

            Text(message)
                .font(AppFont.footnote)
                .foregroundStyle(Palette.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 14) {
                Button(retryTitle, action: onRetry)
                    .buttonStyle(.glass)
                    .accessibilityIdentifier(retryIdentifier)

                if let onDismiss {
                    Button("隐藏", action: onDismiss)
                        .buttonStyle(.glass)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .cardSurface()
    }
}

/// Terminal run outcomes that must stay visible: a budget stop is not a success.
struct RunNoticeRow: View {
    let run: RunDTO

    var body: some View {
        HStack(alignment: .top, spacing: 9) {
            Circle()
                .fill(Palette.status(run.status))
                .frame(width: 7, height: 7)
                .padding(.top, 5)

            VStack(alignment: .leading, spacing: 3) {
                Text("上一次任务\(run.status.displayName)")
                    .font(AppFont.footnote.weight(.medium))
                    .foregroundStyle(Palette.ink)
                if let error = run.error, !error.isEmpty {
                    Text(error)
                        .font(AppFont.caption)
                        .foregroundStyle(Palette.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surfaceMuted, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .accessibilityIdentifier("runNotice")
    }
}

// MARK: - Appearance

enum AppAppearance: String, CaseIterable, Identifiable {
    case system, light, dark

    var id: String { rawValue }

    var title: String {
        switch self {
        case .system: return "跟随系统"
        case .light: return "浅色"
        case .dark: return "深色"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }
}
