//
//  ListPieces.swift
//  cubes
//
//  One row spec for the conversation and team lists, a quiet section header, an empty state
//  that offers the next step, and a liquid glass pill for a conversation that is working.
//

import SwiftUI

enum ListMetrics {
    static let avatar: CGFloat = 44
    static let gap: CGFloat = 12
    static let rowInset: CGFloat = 16
    /// Separators start under the text column, the way system lists do.
    static var textLeading: CGFloat { rowInset + avatar + gap }
}

/// Section header that sits on the canvas, aligned with the row content.
struct ListSectionHeader: View {
    let title: String

    var body: some View {
        Text(title)
            .font(AppFont.footnote.weight(.semibold))
            .foregroundStyle(Palette.inkSecondary)
            .textCase(nil)
            .padding(.leading, ListMetrics.rowInset)
            .padding(.top, 18)
            .padding(.bottom, 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .listRowInsets(EdgeInsets())
            .accessibilityAddTraits(.isHeader)
    }
}

/// Avatar, title with trailing time, then one preview line. Optional trailing status in place
/// of the time when something is running.
struct ListRow<Avatar: View>: View {
    let title: String
    var badge: String?
    let preview: String
    var timestamp: String?
    var status: RowStatus?
    /// Rows that open a detail sheet rather than a conversation show a disclosure chevron.
    var showsChevron = false
    @ViewBuilder let avatar: () -> Avatar

    enum RowStatus: Equatable {
        case running(String)
    }

    var body: some View {
        HStack(spacing: ListMetrics.gap) {
            avatar()
                .frame(width: ListMetrics.avatar, height: ListMetrics.avatar)

            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(title)
                        .font(AppFont.body.weight(.semibold))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                    if let badge {
                        Text(badge)
                            .font(AppFont.caption2.weight(.semibold))
                            .foregroundStyle(Palette.accent)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(Palette.accent.opacity(0.12)))
                    }
                    Spacer(minLength: 8)
                    trailing
                }
                Text(preview)
                    .font(AppFont.subheadline)
                    .foregroundStyle(Palette.inkSecondary)
                    .lineLimit(1)
            }
            .alignmentGuide(.listRowSeparatorLeading) { $0[.leading] }

            if showsChevron {
                Image(systemName: "chevron.right")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Palette.inkTertiary)
                    .accessibilityHidden(true)
            }
        }
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }

    @ViewBuilder
    private var trailing: some View {
        switch status {
        case .running(let label):
            HStack(spacing: 4) {
                Circle().fill(Palette.accent).frame(width: 6, height: 6)
                Text(label)
                    .font(AppFont.caption.weight(.medium))
                    .foregroundStyle(Palette.accent)
                    .lineLimit(1)
            }
            .accessibilityElement(children: .combine)
        case nil:
            if let timestamp, !timestamp.isEmpty {
                Text(timestamp)
                    .font(AppFont.caption.monospacedDigit())
                    .foregroundStyle(Palette.inkTertiary)
            }
        }
    }
}

extension View {
    /// Applies the shared row chrome: canvas background, separator from the text column to the edge.
    func listRowChrome() -> some View {
        listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 0, leading: ListMetrics.rowInset, bottom: 0, trailing: ListMetrics.rowInset))
            .listRowSeparatorTint(Palette.hairline)
            .alignmentGuide(.listRowSeparatorTrailing) { $0[.trailing] }
    }
}

/// Empty state with the action that fills it, instead of a bare instruction paragraph.
struct ListEmptyRow: View {
    let symbol: String
    let message: String
    var actionTitle: String?
    var actionIdentifier: String?
    var action: (() -> Void)?

    var body: some View {
        HStack(spacing: ListMetrics.gap) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .medium))
                .foregroundStyle(Palette.inkSecondary)
                .frame(width: ListMetrics.avatar, height: ListMetrics.avatar)
                .background(Circle().strokeBorder(Palette.inkTertiary.opacity(0.6), style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
            Text(message)
                .font(AppFont.subheadline)
                .foregroundStyle(Palette.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .font(AppFont.subheadline.weight(.semibold))
                    .foregroundStyle(Palette.accent)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                    .accessibilityIdentifier(actionIdentifier ?? "")
            }
        }
        .padding(.vertical, 10)
    }
}

/// A conversation that is working, as one liquid glass pill: who is replying, open, stop.
struct RunningPill: View {
    let title: String
    let status: String
    let isStopping: Bool
    let onOpen: () -> Void
    let onStop: () -> Void

    var body: some View {
        GlassEffectContainer(spacing: 8) {
            HStack(spacing: 8) {
                Button {
                    onOpen()
                } label: {
                    HStack(spacing: 10) {
                        PulseDots(color: Palette.accent)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(title)
                                .font(AppFont.subheadline.weight(.semibold))
                                .foregroundStyle(Palette.ink)
                                .lineLimit(1)
                            Text(status)
                                .font(AppFont.caption)
                                .foregroundStyle(Palette.inkSecondary)
                                .lineLimit(1)
                        }
                        Spacer(minLength: 4)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(Palette.inkTertiary)
                    }
                    .padding(.horizontal, 14)
                    .frame(minHeight: 52)
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: Capsule())
                .accessibilityLabel("\(title)，\(status)")
                .accessibilityHint("打开这个会话")
                .accessibilityIdentifier("viewRunConversation")

                Button(action: onStop) {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Palette.failed)
                        .frame(width: 52, height: 52)
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: Circle())
                .disabled(isStopping)
                .accessibilityLabel(isStopping ? "正在停止" : "停止")
                .accessibilityIdentifier("stopRunFromList")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("runBanner")
    }
}
