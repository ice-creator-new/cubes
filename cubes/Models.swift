//
//  Models.swift
//  cubes
//
//  Codable mirrors of the frozen supervisor-team contract (snake_case on the wire).
//  Decoding is strict: unknown enum values and missing required fields surface as real
//  errors instead of being silently defaulted.
//

import Foundation

// MARK: - Enumerations

enum AgentKind: String, Codable, Hashable, Sendable {
    case supervisor, member

    var displayName: String {
        switch self {
        case .supervisor: return "主管"
        case .member: return "成员"
        }
    }
}

enum ConversationKind: String, Codable, Hashable, Sendable {
    case direct, group
}

enum MessageKind: String, Codable, Hashable, Sendable {
    case user, agent, action
}

enum MessageStatus: String, Codable, Hashable, Sendable {
    case streaming, complete, interrupted
}

enum RunStatus: String, Codable, Hashable, Sendable {
    case running, completed, stopped, error, limited

    var displayName: String {
        switch self {
        case .running: return "进行中"
        case .completed: return "已完成"
        case .stopped: return "已停止"
        case .error: return "出错"
        case .limited: return "已达上限"
        }
    }

    /// Only `running` is an active task for the whole workspace.
    var isActive: Bool { self == .running }

    /// Terminal states that must stay visible so a budget stop is never mistaken for success.
    var needsAttention: Bool {
        switch self {
        case .running, .completed: return false
        case .stopped, .error, .limited: return true
        }
    }
}

enum ActionStatus: String, Codable, Hashable, Sendable {
    case running, completed, error

    var displayName: String {
        switch self {
        case .running: return "进行中"
        case .completed: return "已完成"
        case .error: return "失败"
        }
    }
}

// MARK: - Entities

struct AgentDTO: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var name: String
    var instructions: String
    var kind: AgentKind
    var avatarSeed: String
    var createdAt: String
    var updatedAt: String
}

extension AgentDTO {
    var isSupervisor: Bool { kind == .supervisor }

    /// First non-empty line of the instructions, for compact directory rows.
    var instructionsSummary: String {
        let firstLine = instructions
            .split(separator: "\n", omittingEmptySubsequences: true)
            .first
            .map(String.init) ?? instructions
        let trimmed = firstLine.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? instructions.trimmingCharacters(in: .whitespacesAndNewlines) : trimmed
    }
}

struct ActionDTO: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var runId: String
    var toolCallId: String
    var toolName: String
    var status: ActionStatus
    var summary: String
    var targetAgentId: String?
    var targetConversationId: String?
    var error: String?
}

extension ActionDTO {
    var toolTitle: String {
        switch toolName {
        case "create_agent": return "创建成员"
        case "create_group": return "建立群组"
        case "set_group_members": return "调整群成员"
        case "post_group_message": return "发布任务"
        case "ask_agent": return "咨询成员"
        default: return "工具调用"
        }
    }

    var symbolName: String {
        switch toolName {
        case "create_agent": return "person.badge.plus"
        case "create_group": return "person.3"
        case "set_group_members": return "person.2"
        case "post_group_message": return "text.bubble"
        case "ask_agent": return "bubble.left.and.bubble.right"
        default: return "gearshape"
        }
    }
}

struct MessageDTO: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var conversationId: String
    var authorId: String?
    var authorName: String
    var avatarSeed: String?
    var kind: MessageKind
    var content: String
    var status: MessageStatus
    var seq: Int
    var createdAt: String
    var action: ActionDTO?
    /// Members named with `@` in the body, as resolved by the server.
    var mentions: [String] = []
    /// The message this one answers, when the author was replying to something specific.
    var replyToId: String?
}

extension MessageDTO {
    /// `mentions` is additive on the wire: an older server omits it.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        conversationId = try container.decode(String.self, forKey: .conversationId)
        authorId = try container.decodeIfPresent(String.self, forKey: .authorId)
        authorName = try container.decode(String.self, forKey: .authorName)
        avatarSeed = try container.decodeIfPresent(String.self, forKey: .avatarSeed)
        kind = try container.decode(MessageKind.self, forKey: .kind)
        content = try container.decode(String.self, forKey: .content)
        status = try container.decode(MessageStatus.self, forKey: .status)
        seq = try container.decode(Int.self, forKey: .seq)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        action = try container.decodeIfPresent(ActionDTO.self, forKey: .action)
        mentions = try container.decodeIfPresent([String].self, forKey: .mentions) ?? []
        replyToId = try container.decodeIfPresent(String.self, forKey: .replyToId)
    }

    /// One-line directory preview; action messages describe what happened, not their body.
    var preview: String {
        let text = kind == .action ? (action?.summary ?? content) : content
        let flattened = text
            .replacingOccurrences(of: "\n", with: " ")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return flattened.isEmpty ? "…" : flattened
    }

    var isStreaming: Bool { status == .streaming }
}

struct ConversationDTO: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var kind: ConversationKind
    var title: String
    var purpose: String
    var memberIds: [String]
    var lastMessage: String?
    var createdAt: String
    var updatedAt: String
}

struct RunDTO: Codable, Identifiable, Hashable, Sendable {
    var id: String
    var conversationId: String
    var status: RunStatus
    var currentStep: String?
    var error: String?
    var modelCalls: Int
    var toolCalls: Int
    var createdAt: String
    var updatedAt: String
    var finishedAt: String?
    /// The member whose reply is being produced right now.
    var currentAgentId: String?
    /// Members queued by an `@` mention, in the order they will answer.
    var pendingAgentIds: [String]
    /// Member-triggered hand-offs already taken in this run.
    var hops: Int
}

extension RunDTO {
    /// What the supervisor is doing right now, or the terminal status when it is no longer running.
    var stepText: String {
        let step = currentStep?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return step.isEmpty ? status.displayName : step
    }

    var startedAt: Date? { ISODate.date(from: createdAt) }

    /// Progress the user can read: how far an @ relay has gone, never raw call counters.
    var progressText: String? {
        hops > 0 ? "@ 接力第 \(hops) 轮，最多 8 轮" : nil
    }

    /// Who is answering, in the user's terms.
    func speakerTitle(agents: [AgentDTO]) -> String {
        guard let currentAgentId, let name = agents.first(where: { $0.id == currentAgentId })?.name else {
            return "主管"
        }
        return name
    }

    func pendingNames(agents: [AgentDTO]) -> [String] {
        pendingAgentIds.compactMap { id in agents.first { $0.id == id }?.name }
    }

    /// "「小明」正在回复" with the queue spelled out when something is waiting.
    func statusHeadline(agents: [AgentDTO]) -> String {
        var text = "「\(speakerTitle(agents: agents))」正在回复"
        let queued = pendingNames(agents: agents)
        if !queued.isEmpty { text += " · 接下来：\(queued.joined(separator: "、"))" }
        return text
    }
}

struct WorkspaceDTO: Codable, Hashable, Sendable {
    var agents: [AgentDTO]
    var conversations: [ConversationDTO]
    var supervisorId: String
    var directConversationId: String
    /// Every conversation with a live run; conversations run independently.
    var activeRuns: [RunDTO]
}

extension WorkspaceDTO {
    var supervisor: AgentDTO? { agents.first { $0.id == supervisorId } }

    var directConversation: ConversationDTO? { conversations.first { $0.id == directConversationId } }

    /// Groups in the order the server sent them (newest first).
    var groups: [ConversationDTO] { conversations.filter { $0.kind == .group } }

    var members: [AgentDTO] { agents.filter { $0.kind == .member } }

    func agent(id: String?) -> AgentDTO? {
        guard let id else { return nil }
        return agents.first { $0.id == id }
    }

    func conversation(id: String?) -> ConversationDTO? {
        guard let id else { return nil }
        return conversations.first { $0.id == id }
    }

    /// Member identity resolved through agent IDs; unresolvable legacy IDs are dropped.
    func members(of conversation: ConversationDTO) -> [AgentDTO] {
        conversation.memberIds.compactMap { memberID in agents.first { $0.id == memberID } }
    }

    func groups(of agentID: String) -> [ConversationDTO] {
        conversations.filter { $0.kind == .group && $0.memberIds.contains(agentID) }
    }
}

/// Read-only snapshots of pre-team roles kept on migrated conversations.
struct LegacyRoleDTO: Codable, Hashable, Sendable {
    var name: String
    var instructions: String
    var avatarSeed: String?
}

struct ConversationDetailDTO: Codable, Hashable, Sendable {
    var conversation: ConversationDTO
    var messages: [MessageDTO]
    var latestRun: RunDTO?
    var legacyRoles: [LegacyRoleDTO]
}

extension ConversationDetailDTO {
    var streamingMessage: MessageDTO? { messages.last { $0.status == .streaming } }
}

struct SnapshotDTO: Codable, Hashable, Sendable {
    var workspace: WorkspaceDTO
    var conversation: ConversationDetailDTO?
}

struct DeltaDTO: Codable, Hashable, Sendable {
    var conversationId: String
    var messageId: String
    var text: String
}

struct HealthDTO: Codable, Hashable, Sendable {
    var status: String
    var configured: Bool
    var model: String
    var baseUrl: String
    var configurationError: String?
}

// MARK: - Requests

struct PatchAgentRequest: Encodable, Sendable {
    var name: String?
    var instructions: String?
}

struct CreateAgentRequest: Encodable, Sendable {
    var name: String
    var instructions: String
}

struct PatchConversationRequest: Encodable, Sendable {
    var title: String?
    var purpose: String?
    var memberIds: [String]?

    enum CodingKeys: String, CodingKey {
        case title, purpose
        case memberIds = "member_ids"
    }
}

struct RunCreateRequest: Encodable, Sendable {
    var content: String
    /// Members named in the body; the server also parses `@名字` itself.
    var mentions: [String]?

    enum CodingKeys: String, CodingKey {
        case content, mentions
    }
}

// MARK: - Dates

enum ISODate {
    private static let withFraction: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let plain: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let time: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "HH:mm"
        return formatter
    }()

    private static let day: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日"
        return formatter
    }()

    private static let dayWithTime: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日 HH:mm"
        return formatter
    }()

    private static let dayFull: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "yyyy年M月d日"
        return formatter
    }()

    static func date(from string: String) -> Date? {
        withFraction.date(from: string) ?? plain.date(from: string)
    }

    static func shortTimestamp(_ string: String) -> String {
        guard let value = date(from: string) else { return "" }
        return dayWithTime.string(from: value)
    }

    /// Conversation-list stamp: time of day for today, date for the current year, full date before.
    static func listTimestamp(_ string: String, now: Date = Date()) -> String {
        guard let value = date(from: string) else { return "" }
        let calendar = Calendar.current
        if calendar.isDate(value, inSameDayAs: now) {
            return time.string(from: value)
        }
        if calendar.isDate(value, equalTo: now, toGranularity: .year) {
            return day.string(from: value)
        }
        return dayFull.string(from: value)
    }

    /// Compact elapsed label for a running task.
    static func elapsed(since start: Date, now: Date = Date()) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(start)))
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes)分\(seconds % 60)秒" }
        return "\(minutes / 60)小时\(minutes % 60)分"
    }
}
