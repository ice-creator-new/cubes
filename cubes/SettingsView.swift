//
//  SettingsView.swift
//  cubes
//
//  Backend address, real model configuration status, and the persisted appearance. The URL key
//  and the appearance key are the ones the previous build already stored, so user settings survive.
//

import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var store: TeamStore
    @Environment(\.dismiss) private var dismiss

    @State private var urlText = ""
    @State private var validationError: String?
    @State private var savedConfirmation = false
    @AppStorage("appearance") private var appearance: AppAppearance = .system

    var body: some View {
        NavigationStack {
            Form {
                Section("外观") {
                    Picker("主题", selection: $appearance) {
                        ForEach(AppAppearance.allCases) { mode in
                            Text(mode.title).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("appearancePicker")
                }

                Section {
                    TextField("http://127.0.0.1:8000", text: $urlText)
                        .font(AppFont.mono)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("backendURL")
                } header: {
                    Text("后端地址")
                } footer: {
                    Text("iOS 只访问这个本地服务，模型密钥保存在后端的 .env 里。模拟器可用 127.0.0.1，真机请填写同一局域网内可访问的地址。")
                }

                if let validationError {
                    Section {
                        Label(validationError, systemImage: "exclamationmark.circle")
                            .font(AppFont.footnote)
                            .foregroundStyle(Palette.failed)
                    }
                }

                Section {
                    Button {
                        save()
                    } label: {
                        HStack {
                            Label("保存", systemImage: "checkmark.circle")
                            Spacer()
                            if savedConfirmation {
                                Text("已保存")
                                    .font(AppFont.caption)
                                    .foregroundStyle(Palette.completed)
                            }
                        }
                    }
                    .accessibilityIdentifier("saveBackend")

                    Button {
                        Task { await store.checkHealth() }
                    } label: {
                        Label("重新检查连接", systemImage: "arrow.clockwise")
                    }
                    .accessibilityIdentifier("refreshHealth")
                }

                Section("模型状态") {
                    statusRow("连接", value: connectionText, color: connectionColor)
                    statusRow(
                        "模型已配置",
                        value: (store.health?.configured ?? false) ? "是" : "否",
                        color: (store.health?.configured ?? false) ? Palette.completed : Palette.stopped
                    )
                    statusRow("模型名称", value: modelText, color: Palette.inkSecondary)
                    statusRow("上游地址", value: store.health?.baseUrl ?? "—", color: Palette.inkSecondary)

                    if let error = store.health?.configurationError, !error.isEmpty {
                        Text(error)
                            .font(AppFont.footnote)
                            .foregroundStyle(Palette.stopped)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let error = store.healthError, !error.isEmpty {
                        Text(error)
                            .font(AppFont.footnote)
                            .foregroundStyle(Palette.failed)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    if store.isCheckingHealth {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("正在检查…")
                                .font(AppFont.caption)
                                .foregroundStyle(Palette.inkTertiary)
                        }
                    }
                }
            }
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("完成") { dismiss() }
                        .accessibilityIdentifier("closeSettings")
                }
            }
            .onAppear {
                urlText = store.backendURLString
            }
        }
        .preferredColorScheme(appearance.colorScheme)
    }

    private func statusRow(_ title: String, value: String, color: Color) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(title)
                .font(AppFont.footnote)
                .foregroundStyle(Palette.inkSecondary)
            Spacer(minLength: 12)
            Text(value)
                .font(AppFont.footnote.weight(.medium))
                .foregroundStyle(color)
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var connectionText: String {
        if store.isCheckingHealth { return "检查中" }
        if store.healthError != nil { return "无法连接" }
        if store.health != nil { return "正常" }
        return "未知"
    }

    private var connectionColor: Color {
        if store.healthError != nil { return Palette.failed }
        if store.health != nil { return Palette.completed }
        return Palette.inkTertiary
    }

    private var modelText: String {
        guard let model = store.health?.model, !model.isEmpty else { return "未填写" }
        return model
    }

    private func save() {
        if let error = store.setBackendURL(urlText) {
            validationError = error
            savedConfirmation = false
            return
        }
        validationError = nil
        savedConfirmation = true
        urlText = store.backendURLString
        Task {
            await store.checkHealth()
            await store.loadWorkspace()
            store.startLiveUpdates()
        }
    }
}
