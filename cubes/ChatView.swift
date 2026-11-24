//
//  ChatView.swift
//  cubes
//
//  One conversation: user goals, member replies, structured action records, that conversation's
//  own run status, and the composer with its `@` member picker.
//

import SwiftUI
import UIKit

// MARK: - Accessibility identifiers

/// Accessibility identifiers for the chat controls. Only the chat on top of the navigation stack
/// uses the bare, required names; a conversation still stacked underneath is suffixed so element
/// queries can never match two controls at once.
struct ChatIdentifiers {
    let topmost: Bool

    var input: String { topmost ? "messageInput" : "messageInputBackground" }
    var send: String { topmost ? "sendMessage" : "sendMessageBackground" }
    var stop: String { topmost ? "stopRun" : "stopRunBackground" }
    var mention: String { topmost ? "mentionButton" : "mentionButtonBackground" }
    var title: String { topmost ? "chatTitle" : "chatTitleBackground" }
    var info: String { topmost ? "groupInfo" : "groupInfoBackground" }
    var reload: String { topmost ? "reloadConversation" : "reloadConversationBackground" }
}

struct ChatView: View {
    @EnvironmentObject private var store: TeamStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    let conversationID: String
    /// Only the chat on top of the stack owns the required accessibility identifiers, so stacked
    /// conversations can never produce an ambiguous match.
    let isTopmost: Bool
    let onOpenConversation: (String) -> Void

    @State private var draft = ""
    @State private var memberTarget: MemberTarget?
    @State private var showsGroupDetail = false
    @State private var isNearBottom = true
    @FocusState private var inputFocused: Bool

    private let bottomAnchor = "chat.bottom"

    private var identifiers: ChatIdentifiers { ChatIdentifiers(topmost: isTopmost) }

    private struct MemberTarget: Identifiable { let id: String }

    var body: some View {
        Group {
            if let detail = activeDetail {
                timeline(detail)
            } else {
                loadingState
            }
        }
        .background(Palette.canvas)
        .navigationTitle(conversationTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { toolbarContent }
        .task(id: conversationID) {
            if draft.isEmpty, let saved = store.retainedDraft(for: conversationID) {
                draft = saved
            }
            await store.observe(conversationID: conversationID)
        }
        .onDisappear { store.detach(conversationID: conversationID) }
        .sheet(item: $memberTarget) { target in
            MemberEditorView(agentID: target.id)
        }
        .sheet(isPresented: $showsGroupDetail) {
            GroupDetailView(conversationID: conversationID)
        }
    }

    // MARK: - Conversation identity

    private var conversation: ConversationDTO? { store.conversation(id: conversationID) }

    private var conversationTitle: String { conversation?.title ?? "会话" }

    private var isGroup: Bool { conversation?.kind == .group }

    private var activeDetail: ConversationDetailDTO? {
        guard store.selectedConversationID == conversationID else { return nil }
        return store.detail
    }

    private var run: RunDTO? { store.run(for: conversationID) }

    private var agents: [AgentDTO] { store.workspace?.agents ?? [] }

    /// Every member name in the workspace, so `@名字` is highlighted even before it is in the roster.
    private var mentionNames: Set<String> { Set(agents.map(\.name)) }

    private func openConversationTarget(_ id: String) {
        if id == conversationID {
            if isGroup { showsGroupDetail = true }
        } else {
            onOpenConversation(id)
        }
    }

    private func openMember(named name: String) {
        guard let agent = store.agent(named: name) else { return }
        memberTarget = MemberTarget(id: agent.id)
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            VStack(spacing: 1) {
                Text(conversationTitle)
                    .font(AppFont.section)
                    .lineLimit(1)
                Text(subtitle)
                    .font(AppFont.caption2)
                    .foregroundStyle(Palette.inkTertiary)
                    .lineLimit(1)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier(identifiers.title)
        }
        if isGroup {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    showsGroupDetail = true
                } label: {
                    Image(systemName: "info.circle")
                }
                .accessibilityLabel("群组设置")
                .accessibilityIdentifier(identifiers.info)
            }
        }
    }

    private var subtitle: String {
        guard let conversation else { return "正在读取…" }
        if conversation.kind == .direct {
            if store.member(ofDirectChat: conversation) != nil { return "成员 · 私聊" }
            return "团队负责人"
        }
        let count = store.members(of: conversation).count
        if run != nil { return "\(count) 位成员 · 任务进行中" }
        return "\(count) 位成员"
    }

    // MARK: - Timeline

    private func timeline(_ detail: ConversationDetailDTO) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 16) {
                    if let error = store.streamError {
                        TeamErrorCard(
                            title: "实时连接断了",
                            message: error,
                            onRetry: { Task { await store.reloadSelected() } },
                            onDismiss: { store.dismissStreamError() }
                        )
                    }

                    if let latest = detail.latestRun, latest.status.needsAttention {
                        RunNoticeRow(run: latest)
                    }

                    if !detail.legacyRoles.isEmpty {
                        LegacyRolesCard(roles: detail.legacyRoles)
                    }

                    if detail.messages.isEmpty {
                        EmptyConversationInvite(
                            conversation: detail.conversation,
                            directMember: store.member(ofDirectChat: detail.conversation) ?? store.supervisor,
                            members: store.members(of: detail.conversation),
                            onSuggestion: { suggestion in
                                draft = suggestion
                                inputFocused = true
                            }
                        )
                    }

                    ForEach(detail.messages) { message in
                        ChatMessageRow(
                            message: message,
                            mentionNames: mentionNames,
                            replyToName: replyName(for: message, in: detail.messages),
                            onOpenMember: { memberTarget = MemberTarget(id: $0) },
                            onOpenConversation: openConversationTarget,
                            onMention: openMember(named:),
                            onReply: { startReply(to: $0) }
                        )
                    }

                    if showsThinking(detail) {
                        ThinkingIndicator(
                            title: thinkingTitle,
                            step: run?.stepText ?? "正在分析目标",
                            startedAt: run?.startedAt
                        )
                        .padding(.trailing, 24)
                    }

                    Color.clear
                        .frame(height: 1)
                        .id(bottomAnchor)
                }
                .readableWidth()
                .padding(.horizontal, 18)
                .padding(.top, 14)
                .padding(.bottom, 12)
            }
            .scrollDismissesKeyboard(.interactively)
            .onScrollGeometryChange(for: Bool.self) { geometry in
                let distance = geometry.contentSize.height - geometry.contentOffset.y - geometry.containerSize.height
                return distance < 140
            } action: { _, nearBottom in
                isNearBottom = nearBottom
            }
            .onChange(of: scrollToken) { _, _ in
                guard isNearBottom else { return }
                if reduceMotion {
                    proxy.scrollTo(bottomAnchor, anchor: .bottom)
                } else {
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(bottomAnchor, anchor: .bottom)
                    }
                }
            }
            .task(id: detail.conversation.id) {
                try? await Task.sleep(for: .milliseconds(60))
                proxy.scrollTo(bottomAnchor, anchor: .bottom)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { composer(detail) }
    }

    private func showsThinking(_ detail: ConversationDetailDTO) -> Bool {
        guard run != nil else { return false }
        return detail.streamingMessage == nil
    }

    private var thinkingTitle: String {
        guard let run, let name = agents.first(where: { $0.id == run.currentAgentId })?.name else {
            return "主管正在工作"
        }
        return "「\(name)」正在思考"
    }

    /// Who a reply answers: the author's name, or `你` when it answers the user.
    private func replyName(for message: MessageDTO, in messages: [MessageDTO]) -> String? {
        guard let targetID = message.replyToId else { return nil }
        guard let target = messages.first(where: { $0.id == targetID }) else { return "更早的消息" }
        return target.kind == .user ? "你" : target.authorName
    }

    private var scrollToken: String {
        guard let detail = activeDetail else { return "" }
        let last = detail.messages.last
        return "\(detail.messages.count)|\(last?.id ?? "")|\(last?.content.count ?? 0)|\(last?.status.rawValue ?? "")"
    }

    // MARK: - Composer

    private func composer(_ detail: ConversationDetailDTO) -> some View {
        VStack(spacing: 8) {
            if let run {
                RunBanner(
                    run: run,
                    agents: agents,
                    conversationTitle: nil,
                    isStopping: store.isStopping(conversationID: conversationID),
                    stopIdentifier: identifiers.stop,
                    onOpen: nil,
                    onStop: { Task { await store.stopRun(conversationID: conversationID) } }
                )
            }

            if showsMentionPicker {
                mentionPicker
            }

            HStack(alignment: .bottom, spacing: 6) {
                if !mentionCandidates.isEmpty {
                    Button(action: startMention) {
                        Image(systemName: "at")
                            .font(.system(size: 17, weight: .medium))
                            .foregroundStyle(Palette.inkSecondary)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(isLocked)
                    .accessibilityLabel("点名成员")
                    .accessibilityIdentifier(identifiers.mention)
                }

                TextField(placeholder, text: $draft, axis: .vertical)
                    .font(AppFont.body)
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1...6)
                    .focused($inputFocused)
                    .padding(.leading, mentionCandidates.isEmpty ? 16 : 4)
                    .padding(.vertical, 12)
                    .disabled(isLocked)
                    .accessibilityIdentifier(identifiers.input)

                Button(action: send) {
                    ZStack {
                        if store.isSending(conversationID: conversationID) {
                            ProgressView().controlSize(.small).tint(Palette.onAccent)
                        } else {
                            Image(systemName: "arrow.up")
                                .font(.system(size: 15, weight: .bold))
                                .foregroundStyle(Palette.onAccent)
                        }
                    }
                    .frame(width: 36, height: 36)
                    .background(Circle().fill(canSend ? Palette.ink : Palette.ink.opacity(0.28)))
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .padding(.trailing, 2)
                .accessibilityLabel("发送")
                .accessibilityIdentifier(identifiers.send)
            }
            .glassSurface(.rect(cornerRadius: 26), interactive: false, style: .init(shadow: 0.08))
            .padding(.horizontal, 12)
        }
        .padding(.horizontal, 4)
        .padding(.top, 6)
        .padding(.bottom, 8)
    }

    /// Members this conversation can name: a group's roster, never a private thread.
    private var mentionCandidates: [AgentDTO] {
        guard let conversation, conversation.kind == .group else { return [] }
        return store.members(of: conversation)
    }

    private var openMentionQuery: MentionText.Query? { MentionText.query(in: draft) }

    private var showsMentionPicker: Bool { openMentionQuery != nil }

    private var mentionMatches: [AgentDTO] {
        guard let query = openMentionQuery else { return [] }
        let needle = query.text.lowercased()
        guard !needle.isEmpty else { return mentionCandidates }
        return mentionCandidates.filter { $0.name.lowercased().contains(needle) }
    }

    private var mentionPicker: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                if mentionMatches.isEmpty {
                    Text("没有匹配的成员")
                        .font(AppFont.footnote)
                        .foregroundStyle(Palette.inkTertiary)
                        .padding(.horizontal, 14)
                        .frame(minHeight: 44)
                } else {
                    ForEach(mentionMatches) { agent in
                        Button {
                            pickMention(agent)
                        } label: {
                            HStack(spacing: 6) {
                                AvatarView(
                                    seed: agent.avatarSeed,
                                    size: 26,
                                    identity: agent.name,
                                    mood: store.mood(for: agent.id, conversationID: conversationID)
                                )
                                Text(agent.name)
                                    .font(AppFont.footnote.weight(.medium))
                                    .foregroundStyle(Palette.ink)
                                    .lineLimit(1)
                            }
                            .padding(.horizontal, 12)
                            .frame(minHeight: 44)
                            .glassSurface(.capsule, interactive: true, style: .init(shadow: 0.04))
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("mentionCandidate_\(agent.id)")
                        .accessibilityLabel("点名 \(agent.name)")
                    }
                }
            }
            .padding(.horizontal, 12)
        }
        .frame(height: 52)
    }

    private var isLocked: Bool {
        store.isRunning(conversationID: conversationID) || store.isSending(conversationID: conversationID)
    }

    private var placeholder: String {
        if store.isRunning(conversationID: conversationID) { return "正在回复，稍后再发…" }
        guard let conversation else { return "写下要完成的目标…" }
        if conversation.kind == .direct {
            guard let member = store.member(ofDirectChat: conversation) else {
                return conversation.lastMessage == nil ? "把目标交给主管…" : "继续告诉主管…"
            }
            return conversation.lastMessage == nil ? "直接和「\(member.name)」说…" : "继续和「\(member.name)」说…"
        }
        return conversation.lastMessage == nil
            ? "@ 一位成员，或直接把目标交给主管"
            : "给这个群一个新目标，@ 成员让它先回复"
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isLocked
            && store.selectedConversationID == conversationID
    }

    /// Ids of members still named in the draft; text the user deleted no longer counts.
    private var mentionIDs: [String] {
        MentionText.ids(in: draft, candidates: mentionCandidates.map { (id: $0.id, name: $0.name) })
    }

    private func pickMention(_ agent: AgentDTO) {
        draft = MentionText.inserting(agent.name, into: draft, replacing: openMentionQuery)
        inputFocused = true
    }

    private func startMention() {
        if !draft.isEmpty, let last = draft.last, !last.isWhitespace { draft += " " }
        draft += "@"
        inputFocused = true
    }

    private func startReply(to message: MessageDTO) {
        guard message.kind == .agent else { return }
        draft = MentionText.inserting(message.authorName, into: draft, replacing: openMentionQuery)
        inputFocused = true
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        let mentions = mentionIDs
        inputFocused = false
        Task {
            if await store.send(conversationID: conversationID, content: text, mentions: mentions) == nil,
               draft.trimmingCharacters(in: .whitespacesAndNewlines) == text {
                draft = ""
            }
        }
    }

    // MARK: - Loading and failures

    private var loadingState: some View {
        VStack(spacing: 16) {
            if let error = store.streamError {
                Image(systemName: "antenna.radiowaves.left.and.right.slash")
                    .font(.system(size: 30))
                    .foregroundStyle(Palette.inkTertiary)
                Text("没能打开这个会话")
                    .font(AppFont.section)
                    .foregroundStyle(Palette.ink)
                Text(error)
                    .font(AppFont.footnote)
                    .foregroundStyle(Palette.inkSecondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                Button("重新加载") { Task { await store.reloadSelected() } }
                    .buttonStyle(.glassProminent)
                    .accessibilityIdentifier(identifiers.reload)
            } else {
                ProgressView()
                Text("正在读取会话…")
                    .font(AppFont.footnote)
                    .foregroundStyle(Palette.inkSecondary)
            }
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Message rows

struct ChatMessageRow: View {
    @EnvironmentObject private var store: TeamStore

    let message: MessageDTO
    let mentionNames: Set<String>
    /// Author the message answers ("你" for the user), when it answers one.
    let replyToName: String?
    let onOpenMember: (String) -> Void
    let onOpenConversation: (String) -> Void
    let onMention: (String) -> Void
    let onReply: (MessageDTO) -> Void

    var body: some View {
        Group {
            switch message.kind {
            case .user:
                userBubble
            case .agent:
                agentBlock
            case .action:
                ActionMessageCard(
                    message: message,
                    onOpenMember: onOpenMember,
                    onOpenConversation: onOpenConversation
                )
            }
        }
        .environment(\.openURL, OpenURLAction { url in
            guard let name = MentionLink.name(from: url) else { return .systemAction }
            onMention(name)
            return .handled
        })
        .contextMenu {
            if message.kind == .agent, message.authorId != nil {
                Button {
                    onReply(message)
                } label: {
                    Label("回复", systemImage: "arrowshape.turn.up.left")
                }
            }
            Button {
                UIPasteboard.general.string = copiedText
            } label: {
                Label("复制", systemImage: "doc.on.doc")
            }
        }
        .accessibilityIdentifier("message_\(message.id)")
    }

    private var copiedText: String {
        message.kind == .action ? (message.action?.summary ?? message.content) : message.content
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 56)
            Text(userText)
                .font(AppFont.body)
                .textSelection(.enabled)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background {
                    UnevenRoundedRectangle(
                        topLeadingRadius: 20,
                        bottomLeadingRadius: 20,
                        bottomTrailingRadius: 8,
                        topTrailingRadius: 20,
                        style: .continuous
                    )
                    .stroke(Palette.hairline, lineWidth: 1)
                }
        }
        .accessibilityLabel("你说：\(message.content)")
    }

    /// Mentions stay tappable inside the user's own text, so a typo is one tap from the profile.
    private var userText: AttributedString {
        var string = AttributedString(message.content)
        string.foregroundColor = Palette.ink
        for token in MentionText.tokens(in: message.content) where mentionNames.contains(token.name) {
            guard let lower = AttributedString.Index(token.range.lowerBound, within: string),
                  let upper = AttributedString.Index(token.range.upperBound, within: string)
            else { continue }
            let range = lower..<upper
            string[range].inlinePresentationIntent = .stronglyEmphasized
            string[range].foregroundColor = Palette.accent
            string[range].link = MentionLink.url(for: token.name)
        }
        return string
    }

    private var agentBlock: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 0) {
                if let authorID = message.authorId {
                    Button {
                        onOpenMember(authorID)
                    } label: {
                        authorHeader
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("查看成员 \(message.authorName)")
                } else {
                    authorHeader
                }

                Spacer(minLength: 0)
            }

            if let replyToName {
                Label("回复 \(replyToName == "你" ? "你" : "@\(replyToName)")", systemImage: "arrowshape.turn.up.left")
                    .labelStyle(.titleAndIcon)
                    .font(AppFont.caption)
                    .foregroundStyle(Palette.inkSecondary)
                    .padding(.leading, 38)
            }

            if message.content.isEmpty, message.isStreaming {
                PulseDots(color: Palette.accent)
                    .padding(.leading, 38)
            } else {
                MarkdownBody(text: message.content, isStreaming: message.isStreaming, mentionNames: mentionNames)
                    .padding(.leading, 38)
                    .padding(.trailing, 4)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var authorHeader: some View {
        HStack(spacing: 8) {
            AvatarView(
                seed: message.avatarSeed,
                size: 30,
                identity: message.authorName,
                mood: authorMood
            )
            Text(message.authorName)
                .font(AppFont.footnote.weight(.semibold))
                .foregroundStyle(Palette.ink)
            if let statusLabel {
                Text(statusLabel)
                    .font(AppFont.caption2)
                    .foregroundStyle(Palette.inkTertiary)
            }
        }
        .contentShape(Rectangle())
    }

    private var authorMood: AvatarMood {
        guard let authorID = message.authorId else { return .idle }
        return store.mood(for: authorID, conversationID: message.conversationId)
    }

    private var statusLabel: String? {
        switch message.status {
        case .streaming: return "正在生成"
        case .interrupted: return "已停止"
        case .complete: return nil
        }
    }
}

/// A structured record of one real tool call, linked to the entity it changed.
struct ActionMessageCard: View {
    @EnvironmentObject private var store: TeamStore

    let message: MessageDTO
    let onOpenMember: (String) -> Void
    let onOpenConversation: (String) -> Void

    private var action: ActionDTO? { message.action }

    private var conversationTarget: String? { action?.targetConversationId }

    private var memberTarget: String? { action?.targetAgentId }

    private var hasTarget: Bool { conversationTarget != nil || memberTarget != nil }

    private var targetTitle: String? {
        if let id = conversationTarget { return store.conversation(id: id)?.title }
        if let id = memberTarget { return store.agent(id: id)?.name }
        return nil
    }

    var body: some View {
        Group {
            if hasTarget {
                Button(action: open) { card }
                    .buttonStyle(.plain)
            } else {
                card
            }
        }
        .accessibilityIdentifier("action_\(action?.id ?? message.id)")
    }

    private func open() {
        if let id = conversationTarget {
            onOpenConversation(id)
            return
        }
        if let id = memberTarget {
            onOpenMember(id)
        }
    }

    private var card: some View {
        HStack(alignment: .top, spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(Palette.accent.opacity(0.12))
                Image(systemName: action?.symbolName ?? "gearshape")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Palette.accent)
            }
            .frame(width: 28, height: 28)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(action?.toolTitle ?? "行动")
                        .font(AppFont.footnote.weight(.semibold))
                        .foregroundStyle(Palette.ink)
                    ActionStatusPill(status: action?.status ?? .completed)
                    Spacer(minLength: 0)
                    if hasTarget {
                        Image(systemName: "arrow.up.forward.square")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(Palette.inkTertiary)
                    }
                }

                Text(action?.summary ?? message.content)
                    .font(AppFont.footnote)
                    .foregroundStyle(Palette.inkSecondary)
                    .fixedSize(horizontal: false, vertical: true)

                if let error = action?.error, !error.isEmpty {
                    Text(error)
                        .font(AppFont.caption)
                        .foregroundStyle(Palette.failed)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let targetTitle {
                    Text("打开「\(targetTitle)」")
                        .font(AppFont.caption2)
                        .foregroundStyle(Palette.accent)
                }
            }
        }
        .padding(12)
        .overlay {
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(Palette.hairline, lineWidth: 1)
        }
    }
}

// MARK: - Empty conversation

struct EmptyConversationInvite: View {
    let conversation: ConversationDTO
    let directMember: AgentDTO?
    let members: [AgentDTO]
    let onSuggestion: (String) -> Void

    private static let suggestions = [
        "帮我规划下个版本先做什么",
        "调研一个还没有结论的技术选型",
        "把一份需求拆成可执行的任务"
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if conversation.kind == .direct, let directMember {
                AvatarView(seed: directMember.avatarSeed, size: 72, identity: directMember.name)
                VStack(alignment: .leading, spacing: 6) {
                    Text(directMember.name)
                        .font(AppFont.title)
                        .foregroundStyle(Palette.ink)
                    Text(directMember.instructions)
                        .font(AppFont.footnote)
                        .foregroundStyle(Palette.inkSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                GroupAvatarView(members: members, size: 72)
                Text("「\(conversation.title)」还很安静")
                    .font(AppFont.title)
                    .foregroundStyle(Palette.ink)
                Text(
                    conversation.purpose.isEmpty
                        ? "@ 一位成员，或直接把目标交给主管"
                        : conversation.purpose
                )
                .font(AppFont.footnote)
                .foregroundStyle(Palette.inkSecondary)
                .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("可以试试")
                    .font(AppFont.caption)
                    .foregroundStyle(Palette.inkTertiary)

                ForEach(Array(Self.suggestions.enumerated()), id: \.offset) { index, suggestion in
                    Button {
                        onSuggestion(suggestion)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "text.bubble")
                                .font(.system(size: 12, weight: .medium))
                            Text(suggestion)
                                .font(AppFont.footnote)
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .glassSurface(.capsule, interactive: true, style: .init(shadow: 0.03))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("suggestion_\(index)")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, 18)
    }
}

// MARK: - Migrated history

/// Pre-team roles kept on migrated conversations: readable, never executable.
struct LegacyRolesCard: View {
    let roles: [LegacyRoleDTO]

    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Palette.inkTertiary)
                    Text("迁移前的历史角色（只读）")
                        .font(AppFont.footnote.weight(.medium))
                        .foregroundStyle(Palette.inkSecondary)
                    Spacer(minLength: 0)
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(Palette.inkTertiary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("legacyRoles")

            if expanded {
                ForEach(roles, id: \.self) { role in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(role.name)
                            .font(AppFont.footnote.weight(.semibold))
                            .foregroundStyle(Palette.ink)
                        Text(role.instructions)
                            .font(AppFont.caption)
                            .foregroundStyle(Palette.inkSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Text("这些角色来自迁移前的讨论，只作为历史保留，不会加入现在的团队。")
                    .font(AppFont.caption2)
                    .foregroundStyle(Palette.inkTertiary)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surfaceMuted, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}
