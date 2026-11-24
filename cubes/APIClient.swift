//
//  APIClient.swift
//  cubes
//
//  Thin async HTTP client for the local supervisor-team service, including byte-framed SSE.
//

import Foundation

enum APIError: LocalizedError {
    case invalidURL
    case transport(String)
    case server(status: Int, detail: String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "后端地址无效，请填写形如 http://127.0.0.1:8000 的地址。"
        case .transport(let message):
            return "无法连接后端：\(message)"
        case .server(let status, let detail):
            return detail.isEmpty ? "后端返回错误（HTTP \(status)）。" : detail
        case .decoding(let message):
            return "无法解析后端返回的数据：\(message)"
        }
    }
}

struct SSEEvent: Sendable {
    var name: String
    var data: String
}

struct APIClient {
    let baseURL: URL
    let session: URLSession

    init(baseURL: URL, session: URLSession) {
        self.baseURL = baseURL
        self.session = session
    }

    // MARK: - URL normalisation

    /// Accepts `http`/`https` with a non-empty host. A missing scheme defaults to `http`,
    /// which is what a local development service uses.
    static func normalizeURL(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        guard !text.contains(" ") && !text.contains("\n") else { return nil }
        if !text.contains("://") {
            text = "http://" + text
        }
        guard let url = URL(string: text),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host,
              !host.isEmpty
        else { return nil }
        // Strip a trailing slash so path joining stays predictable.
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        if components?.path == "/" { components?.path = "" }
        return components?.url ?? url
    }

    // MARK: - Routes

    func health() async throws -> HealthDTO {
        try await send("GET", "health")
    }

    func workspace() async throws -> WorkspaceDTO {
        try await send("GET", "workspace")
    }

    func conversation(id: String) async throws -> ConversationDetailDTO {
        try await send("GET", "conversations/\(pathEscape(id))")
    }

    func patchAgent(id: String, name: String?, instructions: String?) async throws -> AgentDTO {
        let body = PatchAgentRequest(name: name, instructions: instructions)
        return try await send("PATCH", "agents/\(pathEscape(id))", body: try encode(body))
    }

    func createAgent(name: String, instructions: String) async throws -> AgentDTO {
        let body = CreateAgentRequest(name: name, instructions: instructions)
        return try await send("POST", "agents", body: try encode(body))
    }

    func patchConversation(
        id: String,
        title: String?,
        purpose: String?,
        memberIDs: [String]?
    ) async throws -> ConversationDTO {
        let body = PatchConversationRequest(title: title, purpose: purpose, memberIds: memberIDs)
        return try await send("PATCH", "conversations/\(pathEscape(id))", body: try encode(body))
    }

    func startRun(conversationID: String, content: String, mentions: [String]) async throws -> RunDTO {
        let body = RunCreateRequest(content: content, mentions: mentions.isEmpty ? nil : mentions)
        return try await send("POST", "conversations/\(pathEscape(conversationID))/runs", body: try encode(body))
    }

    /// Idempotent: returns that member's private conversation, creating it on first use.
    func openDirect(agentID: String) async throws -> ConversationDTO {
        try await send("POST", "agents/\(pathEscape(agentID))/direct", body: Data("{}".utf8))
    }

    func run(id: String) async throws -> RunDTO {
        try await send("GET", "runs/\(pathEscape(id))")
    }

    func stopRun(id: String) async throws -> RunDTO {
        try await send("POST", "runs/\(pathEscape(id))/stop", body: Data("{}".utf8))
    }

    // MARK: - Server-sent events

    func events(conversationID: String?) -> AsyncThrowingStream<SSEEvent, Error> {
        guard var components = URLComponents(
            url: baseURL.appendingPathComponent("events"),
            resolvingAgainstBaseURL: false
        ) else {
            return AsyncThrowingStream { $0.finish(throwing: APIError.invalidURL) }
        }
        if let conversationID {
            components.queryItems = [URLQueryItem(name: "conversation_id", value: conversationID)]
        }
        guard let url = components.url else {
            return AsyncThrowingStream { $0.finish(throwing: APIError.invalidURL) }
        }

        let session = self.session
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = URLRequest(url: url)
                    request.httpMethod = "GET"
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
                    request.cachePolicy = .reloadIgnoringLocalCacheData
                    // Idle tolerance between events: a member model call can legitimately pause,
                    // and server comments keep the connection warm.
                    request.timeoutInterval = 1_800

                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw APIError.transport("没有收到 HTTP 响应。")
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        var data = Data()
                        for try await byte in bytes {
                            data.append(byte)
                            if data.count > 8_192 { break }
                        }
                        throw APIError.server(
                            status: http.statusCode,
                            detail: Self.detail(from: data) ?? "事件流连接被拒绝。"
                        )
                    }

                    // Foundation's AsyncLineSequence drops empty lines on current OSes.
                    // SSE needs those boundaries, so frame directly from the byte stream.
                    var decoder = SSEFrameDecoder()
                    for try await byte in bytes {
                        try Task.checkCancellation()
                        if let event = try decoder.append(byte) {
                            continuation.yield(event)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: - Plumbing

    private func pathEscape(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? value
    }

    private func encode<T: Encodable>(_ value: T) throws -> Data {
        do {
            return try JSONEncoder().encode(value)
        } catch {
            throw APIError.decoding(error.localizedDescription)
        }
    }

    private func send<T: Decodable>(_ method: String, _ path: String, body: Data? = nil) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return try await perform(request)
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        let outcome: (data: Data, response: URLResponse)
        do {
            outcome = try await session.data(for: request)
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
        let data = outcome.data
        let response = outcome.response

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("没有收到 HTTP 响应。")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.server(
                status: http.statusCode,
                detail: Self.detail(from: data) ?? "请求失败（HTTP \(http.statusCode)）。"
            )
        }
        if data.isEmpty {
            throw APIError.decoding("响应内容为空。")
        }
        do {
            return try Self.makeDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.decoding(Self.describe(error))
        }
    }

    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    /// FastAPI errors are `{"detail": "..."}`, sometimes an array of validation objects.
    static func detail(from data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let text = object["detail"] as? String { return text }
        if let items = object["detail"] as? [[String: Any]] {
            let messages = items.compactMap { $0["msg"] as? String }
            if !messages.isEmpty { return messages.joined(separator: "；") }
        }
        return nil
    }

    /// Decodes one SSE payload strictly; a broken frame is a real, surfaced error.
    static func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        do {
            return try makeDecoder().decode(type, from: Data(text.utf8))
        } catch {
            throw APIError.decoding(describe(error))
        }
    }

    static func describe(_ error: Error) -> String {
        if let decoding = error as? DecodingError {
            switch decoding {
            case .keyNotFound(let key, _):
                return "缺少字段 \(key.stringValue)。"
            case .typeMismatch(_, let context):
                return "字段类型不匹配：\(context.codingPath.map(\.stringValue).joined(separator: "."))。"
            case .valueNotFound(_, let context):
                return "字段值为空：\(context.codingPath.map(\.stringValue).joined(separator: "."))。"
            case .dataCorrupted(let context):
                return context.debugDescription
            @unknown default:
                return "数据格式不正确。"
            }
        }
        return error.localizedDescription
    }
}

/// Byte-level SSE framing. `AsyncBytes.lines` drops blank lines on this OS, which are exactly
/// the frame boundaries SSE needs, so lines are assembled one byte at a time.
private struct SSEFrameDecoder {
    private var line = Data()
    private var eventName = "message"
    private var dataLines: [String] = []
    private var previousWasCR = false

    mutating func append(_ byte: UInt8) throws -> SSEEvent? {
        if byte == 10, previousWasCR {
            previousWasCR = false
            return nil
        }
        if byte == 10 || byte == 13 {
            previousWasCR = byte == 13
            return try finishLine()
        }
        previousWasCR = false
        line.append(byte)
        return nil
    }

    private mutating func finishLine() throws -> SSEEvent? {
        defer { line.removeAll(keepingCapacity: true) }
        guard let text = String(data: line, encoding: .utf8) else {
            throw APIError.decoding("事件流包含无效的 UTF-8 文本。")
        }
        if text.isEmpty {
            guard !dataLines.isEmpty else {
                eventName = "message"
                return nil
            }
            let event = SSEEvent(name: eventName, data: dataLines.joined(separator: "\n"))
            eventName = "message"
            dataLines.removeAll(keepingCapacity: true)
            return event
        }
        if text.hasPrefix(":") { return nil }
        if text.hasPrefix("event:") {
            eventName = String(text.dropFirst(6)).trimmingCharacters(in: .whitespaces)
        } else if text.hasPrefix("data:") {
            var value = String(text.dropFirst(5))
            if value.hasPrefix(" ") { value.removeFirst() }
            dataLines.append(value)
        }
        return nil
    }
}

extension APIError {
    var isCancellation: Bool {
        if case .transport(let message) = self, message.contains("cancelled") { return true }
        if case .transport(let message) = self, message.contains("取消") { return true }
        return false
    }
}
