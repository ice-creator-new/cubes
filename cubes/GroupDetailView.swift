//
//  GroupDetailView.swift
//  cubes
//
//  Group identity and membership: real agent IDs only, with the supervisor always included.
//  The service rejects changes while a task is running, and so does this screen.
//

import Foundation
import SwiftUI

struct GroupDetailView: View {
    @EnvironmentObject private var store: TeamStore
    @Environment(\.dismiss) private var dismiss

    let conversationID: String

    @State private var title = ""
    @State private var purpose = ""
    @State private var selectedMemberIDs: Set<String> = []
    @State private var loaded = false
    @State private var isSaving = false
    @State private var alertMessage = ""
    @State private var showsAlert = false

    private var conversation: ConversationDTO? { store.conversation(id: conversationID) }

    /// Only this group's own run blocks membership edits; other conversations never do.
    private var isLocked: Bool { store.isRunning(conversationID: conversationID) }

    private var selectedMembers: [AgentDTO] {
        guard let workspace = store.workspace else { return [] }
        return workspace.agents.filter { selectedMemberIDs.contains($0.id) }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(alignment: .center, spacing: 14) {
                        GroupAvatarView(members: selectedMembers, size: 56)

                        VStack(alignment: .leading, spacing: 8) {
                            TextField("群组名称", text: $title)
                                .font(AppFont.section)
                                .accessibilityIdentifier("groupTitle")

                            Text("\(selectedMemberIDs.count) 位成员")
                                .font(AppFont.caption)
                                .foregroundStyle(Palette.inkTertiary)
                        }
                    }

                    TextField("这个群要解决什么", text: $purpose, axis: .vertical)
                        .font(AppFont.body)
                        .lineLimit(1...3)
                        .accessibilityIdentifier("groupPurpose")
                }

                Section {
                    if let supervisor = store.supervisor {
                        memberRow(supervisor, locked: true)
                    }
                    ForEach(store.members) { agent in
                        memberRow(agent, locked: false)
                    }
                } header: {
                    Text("成员")
                } footer: {
                    Text("主管始终在群里。成员由主管创建，也可以在这里调整；调整只影响之后的协作。")
                }

                if isLocked {
                    Section {
                        Text("这个群的任务还在进行，等结束后再改成员。")
                            .font(AppFont.footnote)
                            .foregroundStyle(Palette.inkTertiary)
                    }
                }
            }
            .navigationTitle("群组")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { dismiss() }
                        .accessibilityIdentifier("closeGroup")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") { save() }
                        .disabled(!canSave)
                        .accessibilityIdentifier("saveGroup")
                }
            }
            .task { load() }
            .onChange(of: store.conversation(id: conversationID)?.id) { _, _ in load() }
            .alert("无法保存群组", isPresented: $showsAlert) {
                Button("好", role: .cancel) {}
            } message: {
                Text(alertMessage)
            }
        }
    }

    private func memberRow(_ agent: AgentDTO, locked: Bool) -> some View {
        let isSelected = selectedMemberIDs.contains(agent.id)
        return Button {
            guard !locked, !isLocked else { return }
            if isSelected {
                selectedMemberIDs.remove(agent.id)
            } else {
                selectedMemberIDs.insert(agent.id)
            }
        } label: {
            HStack(spacing: 12) {
                AvatarView(
                    seed: agent.avatarSeed,
                    size: 38,
                    identity: agent.name,
                    mood: store.mood(for: agent.id, conversationID: conversationID)
                )

                VStack(alignment: .leading, spacing: 2) {
                    Text(agent.name)
                        .font(AppFont.body)
                        .foregroundStyle(Palette.ink)
                    if locked {
                        Text("主管始终在群里")
                            .font(AppFont.caption2)
                            .foregroundStyle(Palette.inkTertiary)
                    }
                }

                Spacer(minLength: 6)

                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 20))
                    .foregroundStyle(isSelected ? Palette.accent : Palette.inkTertiary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(locked || isLocked)
        .accessibilityIdentifier("groupMemberToggle_\(agent.id)")
        .accessibilityLabel(agent.name)
        .accessibilityValue(isSelected ? "已加入" : "未加入")
    }

    private var canSave: Bool {
        guard !isSaving, !isLocked, loaded, let conversation else { return false }
        let trimmedTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedPurpose = purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTitle.isEmpty, !trimmedPurpose.isEmpty else { return false }
        let ids = orderedMemberIDs
        return trimmedTitle != conversation.title
            || trimmedPurpose != conversation.purpose
            || ids != conversation.memberIds
    }

    /// Member order follows the directory so the composition stays stable.
    private var orderedMemberIDs: [String] {
        guard let workspace = store.workspace else { return [] }
        return workspace.agents.map(\.id).filter { selectedMemberIDs.contains($0) }
    }

    private func load() {
        guard !loaded, let conversation else { return }
        loaded = true
        title = conversation.title
        purpose = conversation.purpose
        var ids = Set(conversation.memberIds)
        if let supervisorID = store.supervisor?.id { ids.insert(supervisorID) }
        selectedMemberIDs = ids
    }

    private func save() {
        let trimmedTitle = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedPurpose = purpose.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedTitle.isEmpty, !trimmedPurpose.isEmpty, !isSaving else { return }

        let memberIDs = orderedMemberIDs
        guard !memberIDs.isEmpty else {
            alertMessage = "群组至少要留下主管。"
            showsAlert = true
            return
        }

        isSaving = true
        Task {
            let error = await store.patchConversation(
                id: conversationID,
                title: trimmedTitle,
                purpose: trimmedPurpose,
                memberIDs: memberIDs
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
