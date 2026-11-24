//
//  ConversationsView.swift
//  cubes
//
//  The home list: the supervisor's thread pinned on top, the groups, then member private chats.
//  Rows share one spec with the team list; a working conversation floats as a glass pill.
//

import SwiftUI

enum ChatRoute: Hashable {
    case chat(String)
}

struct ConversationsView: View {
    @EnvironmentObject private var store: TeamStore

    @State private var path: [ChatRoute] = []
    @State private var showSettings = false
    @State private var showMemberPicker = false
    @State private var didAutoOpen = false
    @State private var groupTarget: GroupTarget?
    @State private var notice: String?

    private struct GroupTarget: Identifiable { let id: String }

    var body: some View {
        NavigationStack(path: $path) {
            list
                .navigationTitle("会话")
                .navigationBarTitleDisplayMode(.large)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            showMemberPicker = true
                        } label: {
                            Image(systemName: "square.and.pencil")
                        }
                        .accessibilityLabel("和成员私聊")
                        .accessibilityIdentifier("newDirectChat")
                    }
                    ToolbarSpacer(.fixed, placement: .topBarTrailing)
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            showSettings = true
                        } label: {
                            Image(systemName: "gearshape")
                        }
                        .accessibilityLabel("后端与外观设置")
                        .accessibilityIdentifier("backendSettings")
                    }
                }
                .navigationDestination(for: ChatRoute.self) { route in
                    switch route {
                    case .chat(let conversationID):
                        ChatView(
                            conversationID: conversationID,
                            isTopmost: path.last == route
                        ) { destination in
                            let target = ChatRoute.chat(destination)
                            if let index = path.firstIndex(of: target) {
                                path = Array(path.prefix(index + 1))
                            } else {
                                path.append(target)
                            }
                        }
                    }
                }
                .sheet(isPresented: $showSettings) { SettingsView() }
                .sheet(isPresented: $showMemberPicker) {
                    MemberPickerSheet { agent in
                        showMemberPicker = false
                        Task {
                            if let error = await store.openDirectChat(agentID: agent.id) { notice = error }
                        }
                    }
                }
                .sheet(item: $groupTarget) { target in
                    GroupDetailView(conversationID: target.id)
                }
                .alert(
                    "无法打开私聊",
                    isPresented: Binding(get: { notice != nil }, set: { if !$0 { notice = nil } })
                ) {
                    Button("好", role: .cancel) { notice = nil }
                } message: {
                    Text(notice ?? "")
                }
                .task {
                    await store.loadWorkspace()
                    autoOpenSupervisorChat()
                }
                .onChange(of: store.directConversation?.id) { _, _ in
                    autoOpenSupervisorChat()
                }
                .onChange(of: store.chatRequest) { _, request in
                    guard let request else { return }
                    didAutoOpen = true
                    openFromRequest(request)
                }
                .onChange(of: store.backendURLString) { _, _ in
                    path.removeAll()
                    didAutoOpen = false
                    groupTarget = nil
                    autoOpenSupervisorChat()
                }
        }
    }

    // MARK: - List

    private var list: some View {
        List {
            if !store.activeRuns.isEmpty {
                Section {
                    ForEach(store.activeRuns) { run in
                        RunningPill(
                            title: store.conversation(id: run.conversationId)?.title ?? "会话",
                            status: run.statusHeadline(agents: store.workspace?.agents ?? []),
                            isStopping: store.isStopping(conversationID: run.conversationId),
                            onOpen: { open(.chat(run.conversationId)) },
                            onStop: { Task { await store.stopRun(conversationID: run.conversationId) } }
                        )
                        .listRowInsets(EdgeInsets(top: 4, leading: ListMetrics.rowInset, bottom: 4, trailing: ListMetrics.rowInset))
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                    }
                }
            }

            if let error = store.workspaceError {
                errorSection(title: "读不到工作区", message: error, identifier: "retryWorkspace") {
                    Task { await store.loadWorkspace() }
                }
            } else if let error = store.streamError {
                // The stream and the directory share one backend: one banner is enough.
                errorSection(title: "实时更新断了", message: error, identifier: "retryStream") {
                    store.retryStream()
                }
            }

            supervisorSection
            groupSection
            directSection
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Palette.canvas)
        .accessibilityIdentifier("conversationList")
        .refreshable { await store.loadWorkspace() }
    }

    private func errorSection(
        title: String,
        message: String,
        identifier: String,
        retry: @escaping () -> Void
    ) -> some View {
        Section {
            TeamErrorCard(title: title, message: message, retryIdentifier: identifier, onRetry: retry)
                .listRowInsets(EdgeInsets(top: 6, leading: ListMetrics.rowInset, bottom: 6, trailing: ListMetrics.rowInset))
                .listRowBackground(Color.clear)
                .listRowSeparator(.hidden)
        }
    }

    @ViewBuilder
    private var supervisorSection: some View {
        Section {
            if let supervisor = store.supervisor, let direct = store.directConversation {
                conversationButton(
                    direct,
                    identifier: "supervisorChat",
                    isDirect: true,
                    badge: "负责人",
                    mood: store.mood(for: supervisor.id, conversationID: direct.id)
                )
                .accessibilityHint(supervisor.instructionsSummary)
            } else if store.isLoadingWorkspace {
                loadingRow("正在读取工作区…")
            } else {
                ListEmptyRow(
                    symbol: "wifi.exclamationmark",
                    message: "还没连上工作区",
                    actionTitle: "设置",
                    actionIdentifier: "openSettingsFromList",
                    action: { showSettings = true }
                )
                .listRowChrome()
            }
        }
    }

    @ViewBuilder
    private var groupSection: some View {
        Section {
            if store.groups.isEmpty {
                ListEmptyRow(symbol: "person.3", message: "主管需要协作时会自己建群")
                    .listRowChrome()
            } else {
                ForEach(store.groups) { conversation in
                    conversationButton(conversation, identifier: "groupRow_\(conversation.id)", isDirect: false)
                        .contextMenu {
                            Button {
                                groupTarget = GroupTarget(id: conversation.id)
                            } label: {
                                Label("群组设置", systemImage: "slider.horizontal.3")
                            }
                        }
                }
            }
        } header: {
            ListSectionHeader(title: "群组")
        }
    }

    @ViewBuilder
    private var directSection: some View {
        Section {
            if store.directChats.isEmpty {
                ListEmptyRow(
                    symbol: "bubble.left",
                    message: "直接和某位成员聊",
                    actionTitle: "选成员",
                    actionIdentifier: "pickMemberForChat",
                    action: { showMemberPicker = true }
                )
                .listRowChrome()
            } else {
                ForEach(store.directChats) { conversation in
                    let member = store.member(ofDirectChat: conversation)
                    conversationButton(
                        conversation,
                        identifier: "directChatRow_\(conversation.id)",
                        isDirect: true,
                        mood: member.map { store.mood(for: $0.id, conversationID: conversation.id) } ?? .idle
                    )
                }
            }
        } header: {
            ListSectionHeader(title: "私聊")
        }
    }

    private func loadingRow(_ text: String) -> some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text(text)
                .font(AppFont.subheadline)
                .foregroundStyle(Palette.inkSecondary)
        }
        .padding(.vertical, 14)
        .listRowChrome()
    }

    private func conversationButton(
        _ conversation: ConversationDTO,
        identifier: String,
        isDirect: Bool,
        badge: String? = nil,
        mood: AvatarMood = .idle
    ) -> some View {
        let speaker = store.run(for: conversation.id)?.speakerTitle(agents: store.workspace?.agents ?? [])
        return Button {
            open(.chat(conversation.id))
        } label: {
            ConversationRow(
                conversation: conversation,
                members: store.members(of: conversation),
                isDirect: isDirect,
                badge: badge,
                running: speaker,
                mood: mood
            )
        }
        .buttonStyle(.plain)
        .listRowChrome()
        .accessibilityIdentifier(identifier)
        .accessibilityLabel(conversation.title)
    }

    // MARK: - Opening

    /// Pushing the same chat twice would stack duplicates; going back to an open one pops to it.
    private func open(_ route: ChatRoute) {
        if let index = path.firstIndex(of: route) {
            path = Array(path.prefix(index + 1))
        } else {
            path.append(route)
        }
    }

    private func openFromRequest(_ request: ChatOpenRequest) {
        let route = ChatRoute.chat(request.conversationID)
        if let index = path.firstIndex(of: route) {
            path = Array(path.prefix(index + 1))
        } else {
            // A request comes from another tab: start a fresh stack instead of piling chats up.
            path = [route]
        }
        store.consumeChatRequest(request.id)
    }

    /// The app lands in the supervisor thread; the list is one back tap away.
    private func autoOpenSupervisorChat() {
        guard !didAutoOpen, path.isEmpty, store.chatRequest == nil,
              let direct = store.directConversation
        else { return }
        didAutoOpen = true
        path = [.chat(direct.id)]
    }
}

// MARK: - Row

struct ConversationRow: View {
    let conversation: ConversationDTO
    let members: [AgentDTO]
    let isDirect: Bool
    var badge: String?
    /// Who is replying right now, when this conversation is working.
    var running: String?
    var mood: AvatarMood = .idle

    var body: some View {
        ListRow(
            title: conversation.title,
            badge: badge,
            preview: previewLine,
            timestamp: ISODate.listTimestamp(conversation.updatedAt),
            status: running.map { .running("\($0)回复中") }
        ) {
            if isDirect, let owner = members.first {
                AvatarView(seed: owner.avatarSeed, size: ListMetrics.avatar, identity: owner.name, mood: mood)
            } else {
                GroupAvatarView(members: members, size: ListMetrics.avatar)
            }
        }
    }

    private var previewLine: String {
        if let preview = conversation.lastMessage, !preview.isEmpty { return preview }
        if isDirect {
            return members.first?.isSupervisor == true
                ? "把目标交给主管，它会组建团队并带回结论"
                : "还没有消息"
        }
        return "\(members.count) 位成员 · 还没有消息"
    }
}

// MARK: - Member picker

/// Pick a member to talk to directly; the supervisor's thread is already pinned on the list.
struct MemberPickerSheet: View {
    @EnvironmentObject private var store: TeamStore
    @Environment(\.dismiss) private var dismiss
    let onPick: (AgentDTO) -> Void

    var body: some View {
        NavigationStack {
            List {
                if store.members.isEmpty {
                    ListEmptyRow(symbol: "person.badge.plus", message: "还没有成员，可以在团队页添加")
                        .listRowChrome()
                } else {
                    ForEach(store.members) { agent in
                        Button { onPick(agent) } label: {
                            ListRow(title: agent.name, preview: agent.instructionsSummary) {
                                AvatarView(seed: agent.avatarSeed, size: ListMetrics.avatar, identity: agent.name)
                            }
                        }
                        .buttonStyle(.plain)
                        .listRowChrome()
                        .accessibilityIdentifier("pickMember_\(agent.id)")
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Palette.canvas)
            .navigationTitle("和谁私聊")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }
}
