//
//  CreateMemberView.swift
//  cubes
//
//  A member the user adds directly. The supervisor can still create members on its own.
//

import SwiftUI

struct CreateMemberView: View {
    @EnvironmentObject private var store: TeamStore
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var instructions = ""
    @State private var isSaving = false
    @State private var alertMessage = ""
    @State private var showsAlert = false

    private var canSave: Bool {
        !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !instructions.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !isSaving
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("名称", text: $name)
                        .font(AppFont.section)
                        .accessibilityIdentifier("createMemberName")
                } footer: {
                    Text("这个名称会出现在团队和它发出的消息上。")
                }

                Section {
                    TextEditor(text: $instructions)
                        .font(AppFont.body)
                        .frame(minHeight: 150)
                        .accessibilityIdentifier("createMemberInstructions")
                } header: {
                    Text("职责说明")
                } footer: {
                    Text("主管分派任务时会把这段说明一起交给它。")
                }
            }
            .navigationTitle("新成员")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("添加") { save() }
                        .disabled(!canSave)
                        .accessibilityIdentifier("saveMember")
                }
            }
            .alert("无法添加成员", isPresented: $showsAlert) {
                Button("好", role: .cancel) {}
            } message: {
                Text(alertMessage)
            }
        }
    }

    private func save() {
        isSaving = true
        Task {
            defer { isSaving = false }
            if let error = await store.createMember(name: name, instructions: instructions) {
                alertMessage = error
                showsAlert = true
            } else {
                dismiss()
            }
        }
    }
}
