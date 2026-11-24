//
//  ContentView.swift
//  cubes
//
//  Root: two native tabs (conversations, team) over one store.
//

import SwiftUI

enum RootTab: Hashable {
    case conversations, team
}

struct ContentView: View {
    @StateObject private var store = TeamStore()
    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("appearance") private var appearance: AppAppearance = .system
    @State private var selection: RootTab = .conversations

    var body: some View {
        TabView(selection: $selection) {
            Tab(value: RootTab.conversations) {
                ConversationsView()
            } label: {
                Label("会话", systemImage: "bubble.left.and.bubble.right")
                    .accessibilityIdentifier("conversationsTab")
            }

            Tab(value: RootTab.team) {
                TeamView()
            } label: {
                Label("团队", systemImage: "person.3")
                    .accessibilityIdentifier("teamTab")
            }
        }
        .tint(Palette.accent)
        .environmentObject(store)
        .preferredColorScheme(appearance.colorScheme)
        .task {
            await store.checkHealth()
            await store.loadWorkspace()
            store.startLiveUpdates()
        }
        .onChange(of: store.chatRequest) { _, request in
            guard request != nil else { return }
            selection = .conversations
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .active else { return }
            Task { await store.refreshOnForeground() }
        }
        .alert(
            "无法发送",
            isPresented: Binding(
                get: { store.sendFailure != nil },
                set: { if !$0 { store.dismissSendFailure() } }
            )
        ) {
            Button("好", role: .cancel) { store.dismissSendFailure() }
        } message: {
            Text(store.sendFailure?.message ?? "")
        }
        .alert(
            "无法停止",
            isPresented: Binding(
                get: { store.stopError != nil },
                set: { if !$0 { store.dismissStopError() } }
            )
        ) {
            Button("好", role: .cancel) { store.dismissStopError() }
        } message: {
            Text(store.stopError ?? "")
        }
    }
}

#Preview {
    ContentView()
}
