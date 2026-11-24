//
//  MemberEditorView.swift
//  cubes
//
//  Edits one agent's name and instructions. Changes apply to later model calls; message author
//  snapshots already written are never rewritten.
//

import Foundation
import SwiftUI

struct MemberEditorView: View {
    @EnvironmentObject private var store: TeamStore
    @Environment(\.dismiss) private var dismiss

    let agentID: String

    @State private var name = ""
    @State private var instructions = ""
    @State private var loaded = false
    @State private var isSaving = false
    @State private var alertMessage = ""
    @State private var showsAlert = false

    private var agent: AgentDTO? { store.agent(id: agentID) }

    private var groups: [ConversationDTO] { store.groups(of: agentID) }

    /// The conversation this member is currently answering in, if any, for its working face.
    private var runningConversationID: String? {
        store.activeRuns.first {
            $0.currentAgentId == agentID || $0.pendingAgentIds.contains(agentID)
        }?.conversationId
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(alignment: .top, spacing: 14) {
                        AvatarView(
                            seed: agent?.avatarSeed,
                            size: 64,
                            identity: name,
                            mood: store.mood(for: agentID, conversationID: runningConversationID)
                        )

                        VStack(alignment: .leading, spacing: 8) {
                            TextField("名称", text: $name)
                                .font(AppFont.section)
                                .accessibilityIdentifier("memberName")

                            Text(agent?.kind.displayName ?? "成员")
                                .font(AppFont.caption)
                                .foregroundStyle(Palette.inkTertiary)

                            if agent == nil {
                                Text("这个成员已经不在当前团队里。")
                                    .font(AppFont.caption)
                                    .foregroundStyle(Palette.failed)
                            }
                        }
                    }
                }

                Section {
                    Button {
                        startChat()
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: "bubble.left.and.bubble.right")
                                .font(.system(size: 15, weight: .medium))
                                .foregroundStyle(Palette.accent)
                            Text("发消息")
                                .font(AppFont.body)
                                .foregroundStyle(Palette.ink)
                            Spacer(minLength: 0)
                        }
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(agent == nil)
                    .accessibilityIdentifier("messageMember")
                } footer: {
                    Text("私聊里只有这个成员回复，不需要经过主管。")
                }

                Section {
                    TextEditor(text: $instructions)
                        .font(AppFont.body)
                        .frame(minHeight: 150)
                        .accessibilityIdentifier("memberInstructions")
                } header: {
                    Text("职责说明")
                } footer: {
                    Text("主管会把这段说明、群组记录和具体任务一起交给该成员。")
                }

                Section("参与群组") {
                    if groups.isEmpty {
                        Text("还没有参与任何群组。")
                            .font(AppFont.footnote)
                            .foregroundStyle(Palette.inkTertiary)
                    } else {
                        ForEach(groups) { group in
                            Text(group.title)
                                .font(AppFont.footnote)
                                .foregroundStyle(Palette.ink)
                        }
                    }
                }

                if runningConversationID != nil {
                    Section {
                        Text("它正在回复。修改会在下一次调用时生效。")
                            .font(AppFont.footnote)
                            .foregroundStyle(Palette.inkTertiary)
                    }
                }
            }
            .navigationTitle("成员")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                        .accessibilityIdentifier("closeMember")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { save() }
                        .disabled(!canSave)
                        .accessibilityIdentifier("saveAgent")
                }
            }
            .task { load() }
            .onChange(of: store.agent(id: agentID)?.updatedAt) { _, _ in load() }
            .alert("无法完成操作", isPresented: $showsAlert) {
                Button("好", role: .cancel) {}
            } message: {
                Text(alertMessage)
            }
        }
    }

    private var canSave: Bool {
        guard !isSaving, let agent else { return false }
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedInstructions = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty, !trimmedInstructions.isEmpty else { return false }
        return trimmedName != agent.name || trimmedInstructions != agent.instructions
    }

    private func load() {
        guard !loaded, let agent else { return }
        loaded = true
        name = agent.name
        instructions = agent.instructions
    }

    private func startChat() {
        Task {
            if let error = await store.openDirectChat(agentID: agentID) {
                alertMessage = error
                showsAlert = true
            } else {
                dismiss()
            }
        }
    }

    private func save() {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedInstructions = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty, !trimmedInstructions.isEmpty, !isSaving else { return }

        isSaving = true
        Task {
            let error = await store.patchAgent(
                id: agentID,
                name: trimmedName,
                instructions: trimmedInstructions
            )
            isSaving = false
            if let error {
                alertMessage = error
                showsAlert = true
            } else {
                dismiss()
            }
        }
    }
}
