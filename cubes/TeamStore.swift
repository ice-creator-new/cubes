//
//  TeamStore.swift
//  cubes
//
//  Single source of truth for the supervisor team: backend configuration, the workspace
//  directory, the observed conversation, per-conversation run control and one live event stream.
//

import Foundation
import Combine

@MainActor
final class TeamStore: ObservableObject {

    // MARK: - Backend configuration

    @Published private(set) var backendURLString: String
    @Published private(set) var health: HealthDTO?
    @Published private(set) var healthError: String?
    @Published private(set) var isCheckingHealth = false

    // MARK: - Workspace directory

    @Published private(set) var workspace: WorkspaceDTO?
    @Published private(set) var isLoadingWorkspace = false
    @Published private(set) var workspaceError: String?

    // MARK: - Observed conversation

    @Published private(set) var selectedConversationID: String?
    @Published private(set) var detail: ConversationDetailDTO?
    /// The single live event stream is down; the user retries explicitly.
    @Published private(set) var streamError: String?
    @Published private(set) var sendingConversationIDs: Set<String> = []
    @Published private(set) var stoppingConversationIDs: Set<String> = []
    /// A send that failed after the chat may already have been popped. The text stays until that
    /// conversation accepts a later send, so leaving the screen cannot discard the goal.
    @Published private(set) var sendFailure: SendFailure?
    /// Stop failures are not stream failures: retrying them reloads nothing.
    @Published private(set) var stopError: String?
    /// Short-lived face states (just answered, just cut off, just pulled in).
    @Published private(set) var moodOverrides: [String: AvatarMood] = [:]
    /// A request to open a conversation; the conversations tab owns the navigation stack.
    @Published private(set) var chatRequest: ChatOpenRequest?

    private var retainedDrafts: [String: String] = [:]
    private var moodTokens: [String: Int] = [:]
    private var moodCounter = 0

    // MARK: - Private

    private let urlSession: URLSession
    private var serverURL: URL
    /// Incremented whenever the observed context changes (server switch, conversation switch,
    /// reload). Async results carry the value they were started with and are dropped when it no
    /// longer matches, so a late response can never overwrite a newer screen.
    private var generation = 0
    /// Bumped only when the backend address changes; health and directory results are tied to it.
    private var serverGeneration = 0
    private var workspaceLoad = 0
    /// One app-wide stream: conversation switching never reopens it, because it carries every run.
    private var streamTask: Task<Void, Never>?
    private var streamToken = 0
    /// A broken stream stays broken until the user retries; no silent reconnect loop.
    private var streamFaulted = false

    static let defaultBackendURLString = "http://127.0.0.1:8000"
    private static let backendDefaultsKey = "roundtable.backendURL"

    init() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 86_400
        configuration.waitsForConnectivity = false
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        urlSession = URLSession(configuration: configuration)

        let stored = UserDefaults.standard.string(forKey: Self.backendDefaultsKey)
        let resolved = stored.flatMap(APIClient.normalizeURL)
            ?? APIClient.normalizeURL(Self.defaultBackendURLString)!
        serverURL = resolved
        backendURLString = resolved.absoluteString
    }

    private var api: APIClient {
        APIClient(baseURL: serverURL, session: urlSession)
    }

    /// Read-only base address for native avatar requests.
    var baseURL: URL { serverURL }

    // MARK: - Derived directory

    var supervisor: AgentDTO? { workspace?.supervisor }
    var directConversation: ConversationDTO? { workspace?.directConversation }
    var groups: [ConversationDTO] { workspace?.groups ?? [] }
    var members: [AgentDTO] { workspace?.members ?? [] }
    /// Every conversation with a live run, in server order.
    var activeRuns: [RunDTO] { workspace?.activeRuns ?? [] }

    /// Member direct chats, newest first, supervisor's pinned thread excluded.
    var directChats: [ConversationDTO] {
        (workspace?.conversations ?? [])
            .filter { $0.kind == .direct && $0.id != workspace?.directConversationId }
    }

    func run(for conversationID: String) -> RunDTO? {
        activeRuns.first { $0.conversationId == conversationID }
    }

    func isRunning(conversationID: String) -> Bool {
        run(for: conversationID) != nil
    }

    func isSending(conversationID: String) -> Bool {
        sendingConversationIDs.contains(conversationID)
    }

    func isStopping(conversationID: String) -> Bool {
        stoppingConversationIDs.contains(conversationID)
    }

    func agent(id: String?) -> AgentDTO? { workspace?.agent(id: id) }

    func agent(named name: String) -> AgentDTO? {
        workspace?.agents.first { $0.name == name }
    }

    func conversation(id: String?) -> ConversationDTO? { workspace?.conversation(id: id) }

    func members(of conversation: ConversationDTO) -> [AgentDTO] {
        workspace?.members(of: conversation) ?? []
    }

    func groups(of agentID: String) -> [ConversationDTO] {
        workspace?.groups(of: agentID) ?? []
    }

    /// The member of a direct chat, or `nil` for the supervisor thread and groups.
    func member(ofDirectChat conversation: ConversationDTO) -> AgentDTO? {
        guard conversation.kind == .direct,
              conversation.id != workspace?.directConversationId,
              let agentID = conversation.memberIds.first
        else { return nil }
        return agent(id: agentID)
    }

    // MARK: - Face state

    /// What a member's avatar should show right now: live run state first, then short windows.
    func mood(for agentID: String, conversationID: String?) -> AvatarMood {
        if let override = moodOverrides[agentID] { return override }
        guard let conversationID else { return .idle }
        if detail?.conversation.id == conversationID,
           detail?.streamingMessage?.authorId == agentID {
            return .speaking
        }
        guard let run = run(for: conversationID) else { return .idle }
        if run.currentAgentId == agentID { return .thinking }
        if run.pendingAgentIds.contains(agentID) { return .thinking }
        return .idle
    }

    private func holdMood(_ mood: AvatarMood, for agentID: String, seconds: Double) {
        moodCounter += 1
        let token = moodCounter
        moodTokens[agentID] = token
        moodOverrides[agentID] = mood
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(Int(seconds * 1_000)))
            guard let self, self.moodTokens[agentID] == token else { return }
            self.moodOverrides[agentID] = nil
            self.moodTokens[agentID] = nil
        }
    }

    // MARK: - Backend configuration

    /// Validates, persists and applies a new backend address. Returns a message on failure.
    @discardableResult
    func setBackendURL(_ raw: String) -> String? {
        guard let url = APIClient.normalizeURL(raw) else {
            return "地址无效。请使用 http:// 或 https:// 开头，并包含主机名，例如 http://127.0.0.1:8000。"
        }
        guard url != serverURL else { return nil }
        serverURL = url
        backendURLString = url.absoluteString
        UserDefaults.standard.set(url.absoluteString, forKey: Self.backendDefaultsKey)
        resetForNewBackend()
        return nil
    }

    /// Switching servers must not leave the previous server's data on screen.
    private func resetForNewBackend() {
        serverGeneration += 1
        generation += 1
        stopStream()
        workspace = nil
        workspaceError = nil
        health = nil
        healthError = nil
        selectedConversationID = nil
        detail = nil
        streamError = nil
        streamFaulted = false
        sendFailure = nil
        stopError = nil
        chatRequest = nil
        moodOverrides.removeAll()
        moodTokens.removeAll()
        retainedDrafts.removeAll()
        isLoadingWorkspace = false
        isCheckingHealth = false
        sendingConversationIDs.removeAll()
        stoppingConversationIDs.removeAll()
    }

    func checkHealth() async {
        let token = serverGeneration
        isCheckingHealth = true
        defer { if token == serverGeneration { isCheckingHealth = false } }
        do {
            let value = try await api.health()
            guard token == serverGeneration else { return }
            health = value
            healthError = nil
        } catch {
            guard token == serverGeneration else { return }
            health = nil
            healthError = Self.message(for: error)
        }
    }

    // MARK: - Workspace directory

    func loadWorkspace() async {
        let load = workspaceLoad + 1
        workspaceLoad = load
        let token = serverGeneration
        isLoadingWorkspace = true
        if workspace == nil { workspaceError = nil }
        defer { if load == workspaceLoad, token == serverGeneration { isLoadingWorkspace = false } }
        do {
            let value = try await api.workspace()
            guard load == workspaceLoad, token == serverGeneration else { return }
            applyWorkspace(value)
            workspaceError = nil
        } catch {
            guard load == workspaceLoad, token == serverGeneration else { return }
            workspaceError = Self.message(for: error)
        }
    }

    private func applyWorkspace(_ value: WorkspaceDTO) {
        workspace = value
        // Keep the on-screen conversation's title/members in step with the directory; message
        // bodies are never touched here.
        if let current = detail, let updated = value.conversation(id: current.conversation.id) {
            detail?.conversation = updated
        }
    }

    // MARK: - Observation

    /// Opens a conversation: load its history, then make sure the shared stream is listening.
    func observe(conversationID: String) async {
        if selectedConversationID != conversationID {
            selectedConversationID = conversationID
            detail = nil
        }
        generation += 1
        let token = generation
        streamError = nil
        if detail == nil {
            await loadDetail(conversationID: conversationID, token: token)
        }
        startStream()
    }

    /// Leaving a chat keeps the stream running; only the open history is dropped.
    func detach(conversationID: String) {
        guard selectedConversationID == conversationID else { return }
        guard !isRunning(conversationID: conversationID), !isSending(conversationID: conversationID) else { return }
        selectedConversationID = nil
        detail = nil
    }

    /// Manual recovery: read the open conversation once, then reopen the stream.
    func reloadSelected() async {
        guard let conversationID = selectedConversationID else {
            restartStream()
            return
        }
        streamError = nil
        generation += 1
        let token = generation
        await loadDetail(conversationID: conversationID, token: token)
        restartStream()
    }

    /// Manual retry for the event stream itself.
    func retryStream() {
        streamError = nil
        restartStream()
    }

    func dismissStreamError() {
        streamError = nil
    }

    func retainedDraft(for conversationID: String) -> String? {
        retainedDrafts[conversationID]
    }

    func dismissSendFailure() {
        sendFailure = nil
    }

    func dismissStopError() {
        stopError = nil
    }

    private func loadDetail(conversationID: String, token: Int) async {
        do {
            let value = try await api.conversation(id: conversationID)
            guard token == generation, selectedConversationID == conversationID, !Task.isCancelled else { return }
            detail = value
        } catch {
            guard token == generation, selectedConversationID == conversationID, !Task.isCancelled else { return }
            if !Self.isCancellation(error) {
                streamError = Self.message(for: error)
            }
        }
    }

    /// Opens the shared stream without opening a chat first.
    func startLiveUpdates() {
        startStream()
    }

    private func startStream() {
        guard streamTask == nil, !streamFaulted else { return }
        streamToken += 1
        let token = streamToken
        streamTask = Task { [weak self] in
            await self?.listen(token: token)
        }
    }

    private func restartStream() {
        stopStream()
        streamFaulted = false
        startStream()
    }

    private func stopStream() {
        streamToken += 1
        streamTask?.cancel()
        streamTask = nil
    }

    /// Consumes the one app-wide stream. It never ends by itself, so any ending is an error the
    /// user must see and retry.
    private func listen(token: Int) async {
        let server = serverGeneration
        do {
            for try await event in api.events(conversationID: nil) {
                if Task.isCancelled { return }
                guard token == streamToken, server == serverGeneration else { return }
                guard apply(event, token: token, server: server) else { return }
            }
            guard !Task.isCancelled else { return }
            streamFailed("事件流提前结束，状态可能已经变化。请重新加载。", token: token, server: server)
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled else { return }
            streamFailed(Self.message(for: error), token: token, server: server)
        }
    }

    private func streamFailed(_ message: String, token: Int, server: Int) {
        guard token == streamToken, server == serverGeneration else { return }
        streamTask = nil
        streamFaulted = true
        streamError = message
    }

    /// Applies one SSE frame. Returns `false` when this connection should stop.
    private func apply(_ event: SSEEvent, token: Int, server: Int) -> Bool {
        guard token == streamToken, server == serverGeneration else { return false }
        do {
            switch event.name {

            case "snapshot":
                let snapshot = try APIClient.decode(SnapshotDTO.self, event.data)
                applySnapshot(snapshot)

            case "workspace":
                let value = try APIClient.decode(WorkspaceDTO.self, event.data)
                applyWorkspace(value)

            case "run":
                let value = try APIClient.decode(RunDTO.self, event.data)
                applyRun(value)

            case "message_start", "message_end":
                let message = try APIClient.decode(MessageDTO.self, event.data)
                try applyMessage(message, started: event.name == "message_start")

            case "delta":
                let delta = try APIClient.decode(DeltaDTO.self, event.data)
                try applyDelta(delta)

            default:
                // Additive server events are ignored; every event this contract defines is handled.
                break
            }
            return true
        } catch {
            streamError = Self.message(for: error)
            return false
        }
    }

    private func applySnapshot(_ snapshot: SnapshotDTO) {
        applyWorkspace(snapshot.workspace)
        guard let conversation = snapshot.conversation else { return }
        guard conversation.conversation.id == selectedConversationID else { return }
        detail = conversation
    }

    private func applyRun(_ run: RunDTO) {
        if run.status.isActive {
            if let index = workspace?.activeRuns.firstIndex(where: { $0.id == run.id }) {
                workspace?.activeRuns[index] = run
            } else {
                workspace?.activeRuns.append(run)
            }
        } else {
            workspace?.activeRuns.removeAll { $0.id == run.id }
        }
        if detail?.conversation.id == run.conversationId {
            detail?.latestRun = run
        }
    }

    private func applyMessage(_ message: MessageDTO, started: Bool) throws {
        updateDirectorySummary(message)
        if started {
            for agentID in message.mentions {
                holdMood(.surprised, for: agentID, seconds: 1.4)
            }
        }
        if !started, message.kind == .agent {
            switch message.status {
            case .complete:
                if let authorID = message.authorId {
                    holdMood(.happy, for: authorID, seconds: 2)
                }
            case .interrupted:
                if let authorID = message.authorId {
                    holdMood(.sad, for: authorID, seconds: 3)
                }
            case .streaming:
                break
            }
        }
        guard message.conversationId == selectedConversationID else { return }
        // History is still being fetched; that read already includes this message.
        guard detail != nil else { return }
        if let index = detail?.messages.firstIndex(where: { $0.id == message.id }) {
            detail?.messages[index] = message
        } else {
            detail?.messages.append(message)
            detail?.messages.sort { $0.seq < $1.seq }
        }
    }

    private func applyDelta(_ delta: DeltaDTO) throws {
        // Deltas for other conversations never carry their bodies into the open screen.
        guard delta.conversationId == selectedConversationID else { return }
        guard detail != nil else { return }
        guard let index = detail?.messages.firstIndex(where: { $0.id == delta.messageId }) else {
            throw APIError.decoding("文本增量没有对应的消息。请重新加载会话。")
        }
        detail?.messages[index].content += delta.text
    }

    /// Both the open conversation and other conversations keep live directory summaries.
    private func updateDirectorySummary(_ message: MessageDTO) {
        guard let index = workspace?.conversations.firstIndex(where: { $0.id == message.conversationId }) else { return }
        workspace?.conversations[index].lastMessage = message.preview
        workspace?.conversations[index].updatedAt = message.createdAt
    }

    // MARK: - Sending a goal

    /// Returns `nil` when the goal was accepted, or a user-facing message when it was not.
    /// The caller decides how to present it and keeps the draft on failure.
    func send(conversationID: String, content: String, mentions: [String]) async -> String? {
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "请先写下要完成的目标。" }
        if isRunning(conversationID: conversationID) {
            return "这个会话的任务还在进行。可以停止它，或等它结束。"
        }
        if isSending(conversationID: conversationID) { return "正在发送，请稍候。" }

        let token = serverGeneration
        sendingConversationIDs.insert(conversationID)
        defer { if token == serverGeneration { sendingConversationIDs.remove(conversationID) } }
        streamError = nil

        do {
            let run = try await api.startRun(
                conversationID: conversationID,
                content: trimmed,
                mentions: mentions
            )
            guard token == serverGeneration else {
                return "后端地址已更改，请检查原工作区的任务状态。"
            }
            applyRun(run)
            startStream()
            retainedDrafts[conversationID] = nil
            if sendFailure?.conversationID == conversationID { sendFailure = nil }
            // The run's own message is written server-side without a `message_start`, so the open
            // history is re-read once to show it immediately.
            if selectedConversationID == conversationID {
                generation += 1
                await loadDetail(conversationID: conversationID, token: generation)
            }
            return nil
        } catch {
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            let message = Self.message(for: error)
            if let apiError = error as? APIError, case .server(status: 409, detail: _) = apiError {
                // Someone else started a run in this conversation: refresh to show it.
                await loadWorkspace()
            }
            retainedDrafts[conversationID] = trimmed
            sendFailure = SendFailure(conversationID: conversationID, text: trimmed, message: message)
            return message
        }
    }

    // MARK: - Stopping a conversation's run

    func stopRun(conversationID: String) async {
        guard let run = run(for: conversationID), !isStopping(conversationID: conversationID) else { return }
        let token = serverGeneration
        stoppingConversationIDs.insert(conversationID)
        defer { if token == serverGeneration { stoppingConversationIDs.remove(conversationID) } }
        do {
            let stopped = try await api.stopRun(id: run.id)
            guard token == serverGeneration else { return }
            applyRun(stopped)
            if !stopped.status.isActive {
                await loadWorkspace()
                guard token == serverGeneration else { return }
                if selectedConversationID == conversationID, detail != nil {
                    generation += 1
                    await loadDetail(conversationID: conversationID, token: generation)
                }
            }
        } catch {
            guard token == serverGeneration else { return }
            stopError = Self.message(for: error)
        }
    }

    // MARK: - Opening chats

    /// Idempotent on the server: opens (or creates) that member's private conversation.
    /// Returns `nil` on success, otherwise the message to present.
    func openDirectChat(agentID: String) async -> String? {
        let token = serverGeneration
        do {
            let conversation = try await api.openDirect(agentID: agentID)
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            if let index = workspace?.conversations.firstIndex(where: { $0.id == conversation.id }) {
                workspace?.conversations[index] = conversation
            } else {
                workspace?.conversations.append(conversation)
            }
            openChat(conversation.id)
            return nil
        } catch {
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            return Self.message(for: error)
        }
    }

    func openChat(_ conversationID: String) {
        chatRequest = ChatOpenRequest(conversationID: conversationID)
    }

    func consumeChatRequest(_ id: UUID) {
        guard chatRequest?.id == id else { return }
        chatRequest = nil
    }

    // MARK: - Editing the team

    /// Returns `nil` on success, otherwise the message to present.
    func patchAgent(id: String, name: String, instructions: String) async -> String? {
        let token = serverGeneration
        do {
            let updated = try await api.patchAgent(id: id, name: name, instructions: instructions)
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            applyAgent(updated)
            await loadWorkspace()
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            if updated.kind == .supervisor, let conversationID = selectedConversationID, detail != nil {
                // The direct thread's title follows the supervisor's name.
                generation += 1
                await loadDetail(conversationID: conversationID, token: generation)
            }
            return nil
        } catch {
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            return Self.message(for: error)
        }
    }

    /// Returns `nil` on success, otherwise the message to present.
    func createMember(name: String, instructions: String) async -> String? {
        let trimmedName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedInstructions = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedName.isEmpty else { return "请先写下成员名称。" }
        guard !trimmedInstructions.isEmpty else { return "请先写下这个成员负责什么。" }
        let token = serverGeneration
        do {
            let created = try await api.createAgent(name: trimmedName, instructions: trimmedInstructions)
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            await loadWorkspace()
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            if workspace?.agent(id: created.id) == nil {
                workspace?.agents.append(created)
            }
            return nil
        } catch {
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            return Self.message(for: error)
        }
    }

    /// Returns `nil` on success, otherwise the message to present.
    func patchConversation(
        id: String,
        title: String,
        purpose: String,
        memberIDs: [String]
    ) async -> String? {
        if isRunning(conversationID: id) {
            return "这个群的任务还在进行，等它结束后再改成员。"
        }
        let token = serverGeneration
        do {
            let updated = try await api.patchConversation(
                id: id,
                title: title,
                purpose: purpose,
                memberIDs: memberIDs
            )
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            applyConversation(updated)
            await loadWorkspace()
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            return nil
        } catch {
            guard token == serverGeneration else { return "后端地址已更改，请重新操作。" }
            return Self.message(for: error)
        }
    }

    private func applyAgent(_ agent: AgentDTO) {
        guard let index = workspace?.agents.firstIndex(where: { $0.id == agent.id }) else { return }
        workspace?.agents[index] = agent
    }

    private func applyConversation(_ conversation: ConversationDTO) {
        guard let index = workspace?.conversations.firstIndex(where: { $0.id == conversation.id }) else { return }
        workspace?.conversations[index] = conversation
        if detail?.conversation.id == conversation.id {
            detail?.conversation = conversation
        }
    }

    // MARK: - Returning to the foreground

    /// The OS may have dropped a suspended stream, so the directory is re-read on every wake.
    func refreshOnForeground() async {
        await loadWorkspace()
        if streamTask == nil, !streamFaulted {
            restartStream()
        }
        if let conversationID = selectedConversationID, detail == nil {
            generation += 1
            await loadDetail(conversationID: conversationID, token: generation)
        }
    }

    // MARK: - Helpers

    private static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let apiError = error as? APIError { return apiError.isCancellation }
        let nsError = error as NSError
        return nsError.code == NSURLErrorCancelled
    }

    nonisolated static func message(for error: Error) -> String {
        if let apiError = error as? APIError { return apiError.errorDescription ?? "\(error)" }
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cannotConnectToHost, .cannotFindHost, .networkConnectionLost:
                return "连不上后端服务。请确认本地服务已启动，并且地址填写正确。"
            case .timedOut:
                return "请求超时。后端可能正在等待模型响应。"
            case .notConnectedToInternet:
                return "设备当前没有网络连接。"
            default:
                return "网络错误：\(urlError.localizedDescription)"
            }
        }
        return error.localizedDescription
    }
}

/// A rejected goal that must survive leaving the conversation that typed it.
struct SendFailure: Equatable {
    var conversationID: String
    var text: String
    var message: String
}

/// A pending request to open a conversation from another tab.
struct ChatOpenRequest: Identifiable, Equatable {
    let id = UUID()
    let conversationID: String
}
