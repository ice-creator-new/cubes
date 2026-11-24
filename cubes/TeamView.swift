//
//  TeamView.swift
//  cubes
//
//  The team directory: the one supervisor plus every member it created, with real Blobatar faces.
//  Editing a member opens the member editor; any row can start a private chat.
//

import SwiftUI

struct TeamView: View {
    @EnvironmentObject private var store: TeamStore

    @State private var showSettings = false
    @State private var showsCreator = false
    @State private var memberTarget: MemberTarget?
    @State private var notice: String?
    private struct MemberTarget: Identifiable { let id: String }

    var body: some View {
        NavigationStack {
            List {
                if !store.activeRuns.isEmpty {
                    Section {
                        ForEach(store.activeRuns) { run in
                            RunningPill(
                                title: store.conversation(id: run.conversationId)?.title ?? "会话",
                                status: run.statusHeadline(agents: store.workspace?.agents ?? []),
                                isStopping: store.isStopping(conversationID: run.conversationId),
                                onOpen: { store.openChat(run.conversationId) },
                                onStop: { Task { await store.stopRun(conversationID: run.conversationId) } }
                            )
                            .listRowInsets(EdgeInsets(top: 4, leading: ListMetrics.rowInset, bottom: 4, trailing: ListMetrics.rowInset))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                        }
                    }
                }

                if let error = store.workspaceError {
                    Section {
                        TeamErrorCard(
                            title: "读不到团队",
                            message: error,
                            retryIdentifier: "retryWorkspaceTeam",
                            onRetry: { Task { await store.loadWorkspace() } }
                        )
                        .listRowInsets(EdgeInsets(top: 6, leading: ListMetrics.rowInset, bottom: 6, trailing: ListMetrics.rowInset))
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                    }
                }

                supervisorSection
                memberSection
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Palette.canvas)
            .navigationTitle("团队")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showsCreator = true
                    } label: {
                        Image(systemName: "person.badge.plus")
                    }
                    .accessibilityLabel("添加成员")
                    .accessibilityIdentifier("createMember")
                }
                ToolbarSpacer(.fixed, placement: .topBarTrailing)
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showSettings = true
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("后端与外观设置")
                    .accessibilityIdentifier("teamBackendSettings")
                }
            }
            .sheet(item: $memberTarget) { target in
                MemberEditorView(agentID: target.id)
            }
            .sheet(isPresented: $showsCreator) { CreateMemberView() }
            .refreshable { await store.loadWorkspace() }
            .task { await store.loadWorkspace() }
            .alert(
                "无法打开私聊",
                isPresented: Binding(
                    get: { notice != nil },
                    set: { if !$0 { notice = nil } }
                )
            ) {
                Button("好", role: .cancel) { notice = nil }
            } message: {
                Text(notice ?? "")
            }
        }
    }

    @ViewBuilder
    private var supervisorSection: some View {
        Section {
            if let supervisor = store.supervisor {
                agentButton(supervisor, preview: supervisor.instructionsSummary)
            } else if store.isLoadingWorkspace {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text("正在读取团队…")
                        .font(AppFont.subheadline)
                        .foregroundStyle(Palette.inkSecondary)
                }
                .padding(.vertical, 14)
                .listRowChrome()
            } else {
                ListEmptyRow(
                    symbol: "wifi.exclamationmark",
                    message: "还没连上工作区",
                    actionTitle: "设置",
                    actionIdentifier: "openSettingsFromTeam",
                    action: { showSettings = true }
                )
                .listRowChrome()
            }
        } header: {
            ListSectionHeader(title: "负责人")
        }
    }

    @ViewBuilder
    private var memberSection: some View {
        Section {
            if store.members.isEmpty {
                ListEmptyRow(
                    symbol: "person.badge.plus",
                    message: "主管会按需创建成员，你也可以先加一位",
                    actionTitle: "添加",
                    actionIdentifier: "createMemberFromEmpty",
                    action: { showsCreator = true }
                )
                .listRowChrome()
            } else {
                ForEach(store.members) { agent in
                    agentButton(agent, preview: memberPreview(agent))
                }
            }
        } header: {
            ListSectionHeader(title: "成员")
        }
    }

    /// What a member does, and where it works, in one line.
    private func memberPreview(_ agent: AgentDTO) -> String {
        let groups = store.groups(of: agent.id)
        guard let first = groups.first else { return agent.instructionsSummary }
        let place = groups.count > 1 ? "\(first.title) 等 \(groups.count) 个群" : first.title
        return "\(place) · \(agent.instructionsSummary)"
    }

    private func agentButton(_ agent: AgentDTO, badge: String? = nil, preview: String) -> some View {
        let busyIn = runningConversation(of: agent.id)
        return Button {
            memberTarget = MemberTarget(id: agent.id)
        } label: {
            ListRow(
                title: agent.name,
                badge: badge,
                preview: preview,
                status: busyIn.map { _ in .running("工作中") },
                showsChevron: true
            ) {
                AvatarView(
                    seed: agent.avatarSeed,
                    size: ListMetrics.avatar,
                    identity: agent.name,
                    mood: store.mood(for: agent.id, conversationID: busyIn)
                )
            }
        }
        .buttonStyle(.plain)
        .listRowChrome()
        .accessibilityIdentifier("agentRow_\(agent.id)")
        .accessibilityLabel("\(agent.name)，\(agent.kind.displayName)")
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button {
                startChat(with: agent)
            } label: {
                Label("发消息", systemImage: "bubble.left")
            }
            .tint(Palette.accent)
            .accessibilityIdentifier("messageAgent_\(agent.id)")
        }
        .contextMenu {
            Button {
                startChat(with: agent)
            } label: {
                Label("发消息", systemImage: "bubble.left")
            }
        }
    }

    /// A member can only be busy in one conversation the caller can see; its thinking face lives there.
    private func runningConversation(of agentID: String) -> String? {
        store.activeRuns.first {
            $0.currentAgentId == agentID || $0.pendingAgentIds.contains(agentID)
        }?.conversationId
    }

    private func startChat(with agent: AgentDTO) {
        Task {
            if let error = await store.openDirectChat(agentID: agent.id) {
                notice = error
            }
        }
    }
}

