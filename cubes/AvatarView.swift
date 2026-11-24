//
//  AvatarView.swift
//  cubes
//
//  Native Blobatar faces. The layered JSON endpoint hands back the body and the expression eyes
//  as vector paths, so a face can breathe, blink, glance and bob on its own; the immutable PNG
//  stays as the fallback. Both caches are keyed by backend URL and seed, and every failure is
//  shown honestly with a tap-to-retry affordance instead of a stand-in face.
//

import SwiftUI
import UIKit

/// One URLSession for faces and PNGs so both share the same on-disk cache ("cubes-avatars").
enum AvatarTransport {
    static let session: URLSession = {
        let configuration = URLSessionConfiguration.default
        configuration.urlCache = URLCache(
            memoryCapacity: 16 << 20,
            diskCapacity: 256 << 20,
            diskPath: "cubes-avatars"
        )
        configuration.requestCachePolicy = .useProtocolCachePolicy
        configuration.timeoutIntervalForRequest = 20
        configuration.waitsForConnectivity = false
        return URLSession(configuration: configuration)
    }()
}

@MainActor
final class AvatarLoader {
    static let shared = AvatarLoader()

    private let cache = NSCache<NSString, UIImage>()
    private var inFlight: [String: Task<UIImage, Error>] = [:]
    private let session: URLSession

    init() {
        session = AvatarTransport.session
        cache.countLimit = 256
    }

    /// Synchronous hit for views that re-appear while scrolling.
    func cached(baseURL: URL, seed: String) -> UIImage? {
        cache.object(forKey: Self.key(baseURL: baseURL, seed: seed) as NSString)
    }

    func image(baseURL: URL, seed: String) async throws -> UIImage {
        let key = Self.key(baseURL: baseURL, seed: seed)
        if let cached = cache.object(forKey: key as NSString) { return cached }
        if let pending = inFlight[key] { return try await pending.value }

        let task = Task<UIImage, Error> { [session] in
            var components = URLComponents(url: baseURL.appending(path: "avatars").appending(path: "\(seed).png"), resolvingAgainstBaseURL: false)
            components?.queryItems = [URLQueryItem(name: "v", value: "blob")]
            guard let url = components?.url else { throw APIError.invalidURL }
            var request = URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 20)
            request.setValue("image/png", forHTTPHeaderField: "Accept")
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw APIError.transport("没有收到 HTTP 响应。")
            }
            guard (200..<300).contains(http.statusCode) else {
                throw APIError.server(
                    status: http.statusCode,
                    detail: APIClient.detail(from: data) ?? "头像服务暂时不可用。"
                )
            }
            guard let image = UIImage(data: data) else {
                throw APIError.decoding("头像图片无法解析。")
            }
            return image
        }

        inFlight[key] = task
        defer { inFlight[key] = nil }
        let image = try await task.value
        cache.setObject(image, forKey: key as NSString)
        return image
    }

    private static func key(baseURL: URL, seed: String) -> String {
        "\(baseURL.absoluteString)|\(seed)"
    }
}

// MARK: - Wire model

/// `GET /avatars/{seed}/face.json` in a 100×100 viewBox.
struct AvatarFace: Decodable, Sendable {
    struct Body: Decodable, Sendable {
        var fill: String
        var d: String
    }

    var seed: String
    var body: Body
    var eyeFill: String
    /// Expression name → SVG path `d` strings (one per eye). `idle` is always present.
    var expressions: [String: [String]]
}

struct AvatarFaceError: LocalizedError {
    let detail: String
    var errorDescription: String? { detail }
}

/// A face parsed once per seed into drawable geometry.
struct AvatarFaceRender {
    struct Eye {
        let path: Path
        let center: CGPoint
    }

    let bodyFill: Color
    let eyeFill: Color
    let body: Path
    let eyes: [AvatarMood: [Eye]]

    init(face: AvatarFace) throws {
        bodyFill = try AvatarFaceRender.color(face.body.fill)
        eyeFill = try AvatarFaceRender.color(face.eyeFill)
        body = try SVGPathParser.path(face.body.d)

        var parsed: [AvatarMood: [Eye]] = [:]
        for (name, paths) in face.expressions {
            guard let mood = AvatarMood(rawValue: name) else { continue }
            parsed[mood] = try paths.map { d in
                let path = try SVGPathParser.path(d)
                let box = path.boundingRect
                return Eye(path: path, center: CGPoint(x: box.midX, y: box.midY))
            }
        }
        guard parsed[.idle] != nil else {
            throw AvatarFaceError(detail: "头像表情缺少基础表情。")
        }
        eyes = parsed
    }

    private static func color(_ text: String) throws -> Color {
        var hex = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if hex.hasPrefix("#") { hex.removeFirst() }
        guard hex.count == 6 || hex.count == 8, let value = UInt32(hex, radix: 16) else {
            throw AvatarFaceError(detail: "头像颜色 \(text.isEmpty ? "(空)" : text)无法解析。")
        }
        let hasAlpha = hex.count == 8
        let red = CGFloat((value >> (hasAlpha ? 24 : 16)) & 0xFF) / 255
        let green = CGFloat((value >> (hasAlpha ? 16 : 8)) & 0xFF) / 255
        let blue = CGFloat((value >> (hasAlpha ? 8 : 0)) & 0xFF) / 255
        let alpha = hasAlpha ? CGFloat(value & 0xFF) / 255 : 1
        return Color(uiColor: UIColor(red: red, green: green, blue: blue, alpha: alpha))
    }
}

@MainActor
final class AvatarFaceLoader {
    static let shared = AvatarFaceLoader()

    private var renders: [String: AvatarFaceRender] = [:]
    private var order: [String] = []
    private var inFlight: [String: Task<AvatarFace, Error>] = [:]
    private let limit = 256

    /// Synchronous hit for views that re-appear while scrolling.
    func cached(baseURL: URL, seed: String) -> AvatarFaceRender? {
        renders[Self.key(baseURL: baseURL, seed: seed)]
    }

    func render(baseURL: URL, seed: String) async throws -> AvatarFaceRender {
        let key = Self.key(baseURL: baseURL, seed: seed)
        if let cached = renders[key] { return cached }

        let task: Task<AvatarFace, Error>
        if let pending = inFlight[key] {
            task = pending
        } else {
            let url = baseURL.appending(path: "avatars").appending(path: seed).appending(path: "face.json")
            task = Task<AvatarFace, Error> {
                var request = URLRequest(url: url, cachePolicy: .useProtocolCachePolicy, timeoutInterval: 20)
                request.setValue("application/json", forHTTPHeaderField: "Accept")
                let (data, response) = try await AvatarTransport.session.data(for: request)
                guard let http = response as? HTTPURLResponse else {
                    throw APIError.transport("没有收到 HTTP 响应。")
                }
                guard (200..<300).contains(http.statusCode) else {
                    throw APIError.server(
                        status: http.statusCode,
                        detail: APIClient.detail(from: data) ?? "头像表情暂时不可用。"
                    )
                }
                do {
                    return try APIClient.makeDecoder().decode(AvatarFace.self, from: data)
                } catch {
                    throw APIError.decoding("头像表情无法解析。")
                }
            }
            inFlight[key] = task
        }

        let face = try await task.value
        inFlight[key] = nil
        let render = try AvatarFaceRender(face: face)
        store(render, key: key)
        return render
    }

    private func store(_ render: AvatarFaceRender, key: String) {
        if renders[key] == nil { order.append(key) }
        renders[key] = render
        while order.count > limit {
            renders.removeValue(forKey: order.removeFirst())
        }
    }

    private static func key(baseURL: URL, seed: String) -> String {
        "\(baseURL.absoluteString)|\(seed)"
    }
}

// MARK: - SVG paths

/// Minimal SVG path reader covering what Blobatar emits (`M`, `C`, `Z`) plus `L`, `H`, `V`, `Q`,
/// `S` and `T`. Arcs are rejected rather than approximated silently.
enum SVGPathParser {
    static func path(_ d: String) throws -> Path {
        var reader = Reader(d)
        var path = Path()
        var current = CGPoint.zero
        var subpath = CGPoint.zero
        var command: Character?
        var previousCommand: Character?
        var lastCubic: CGPoint?
        var lastQuad: CGPoint?

        while true {
            reader.skipSeparators()
            guard let byte = reader.peek else { break }

            if let letter = Self.letter(byte) {
                guard Self.supported.contains(letter) else {
                    throw AvatarFaceError(detail: "头像路径里出现了不支持的绘制命令 \(letter)。")
                }
                reader.advance()
                command = letter
            } else if command == nil {
                throw AvatarFaceError(detail: "头像路径格式不对：缺少绘制命令。")
            }

            guard let cmd = command else { break }
            let relative = cmd.isLowercase
            var nextCommand: Character? = cmd

            switch Character(cmd.uppercased()) {
            case "M":
                let point = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                path.move(to: point)
                current = point
                subpath = point
                nextCommand = relative ? "l" : "L"
                previousCommand = "M"
            case "L":
                let point = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                path.addLine(to: point)
                current = point
                previousCommand = "L"
            case "H":
                let x = (relative ? current.x : 0) + (try reader.number())
                current = CGPoint(x: x, y: current.y)
                path.addLine(to: current)
                previousCommand = "H"
            case "V":
                let y = (relative ? current.y : 0) + (try reader.number())
                current = CGPoint(x: current.x, y: y)
                path.addLine(to: current)
                previousCommand = "V"
            case "C":
                let control1 = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                let control2 = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                let end = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                path.addCurve(to: end, control1: control1, control2: control2)
                current = end
                lastCubic = control2
                previousCommand = "C"
            case "S":
                let control1 = (previousCommand == "C" || previousCommand == "S") && lastCubic != nil
                    ? CGPoint(x: 2 * current.x - lastCubic!.x, y: 2 * current.y - lastCubic!.y)
                    : current
                let control2 = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                let end = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                path.addCurve(to: end, control1: control1, control2: control2)
                current = end
                lastCubic = control2
                previousCommand = "S"
            case "Q":
                let control = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                let end = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                path.addQuadCurve(to: end, control: control)
                current = end
                lastQuad = control
                previousCommand = "Q"
            case "T":
                let control = (previousCommand == "Q" || previousCommand == "T") && lastQuad != nil
                    ? CGPoint(x: 2 * current.x - lastQuad!.x, y: 2 * current.y - lastQuad!.y)
                    : current
                let end = CGPoint(
                    x: (relative ? current.x : 0) + (try reader.number()),
                    y: (relative ? current.y : 0) + (try reader.number())
                )
                path.addQuadCurve(to: end, control: control)
                current = end
                lastQuad = control
                previousCommand = "T"
            case "Z":
                path.closeSubpath()
                current = subpath
                nextCommand = nil
                previousCommand = "Z"
            default:
                throw AvatarFaceError(detail: "头像路径里出现了不支持的绘制命令 \(cmd)。")
            }

            command = nextCommand
        }
        return path
    }

    private static let supported: Set<Character> = ["M", "m", "L", "l", "H", "h", "V", "v", "C", "c", "S", "s", "Q", "q", "T", "t", "Z", "z"]

    private static func letter(_ byte: UInt8) -> Character? {
        guard (byte >= 0x41 && byte <= 0x5A) || (byte >= 0x61 && byte <= 0x7A) else { return nil }
        return Character(UnicodeScalar(byte))
    }

    private struct Reader {
        private let bytes: [UInt8]
        private var index = 0

        init(_ text: String) { bytes = Array(text.utf8) }

        var peek: UInt8? { index < bytes.count ? bytes[index] : nil }

        mutating func advance() { index += 1 }

        mutating func skipSeparators() {
            while let byte = peek, byte == 0x20 || byte == 0x2C || byte == 0x09 || byte == 0x0A || byte == 0x0D || byte == 0x0C {
                index += 1
            }
        }

        mutating func number() throws -> CGFloat {
            skipSeparators()
            var sign = 1.0
            if peek == 0x2D {
                sign = -1
                index += 1
            } else if peek == 0x2B {
                index += 1
            }

            var value = 0.0
            var seenDigit = false
            while let byte = peek, byte >= 0x30, byte <= 0x39 {
                value = value * 10 + Double(byte - 0x30)
                index += 1
                seenDigit = true
            }
            if peek == 0x2E {
                index += 1
                var factor = 0.1
                while let byte = peek, byte >= 0x30, byte <= 0x39 {
                    value += Double(byte - 0x30) * factor
                    factor /= 10
                    index += 1
                    seenDigit = true
                }
            }
            guard seenDigit else {
                throw AvatarFaceError(detail: "头像路径里的数字无法解析。")
            }
            if peek == 0x65 || peek == 0x45 {
                index += 1
                var exponentSign = 1.0
                if peek == 0x2D {
                    exponentSign = -1
                    index += 1
                } else if peek == 0x2B {
                    index += 1
                }
                var exponent = 0.0
                var seenExponent = false
                while let byte = peek, byte >= 0x30, byte <= 0x39 {
                    exponent = exponent * 10 + Double(byte - 0x30)
                    index += 1
                    seenExponent = true
                }
                if seenExponent {
                    value *= pow(10, exponentSign * exponent)
                }
            }
            return CGFloat(sign * value)
        }
    }
}

// MARK: - Face art

private extension AvatarMood {
    /// The expression a mood wears. Speaking keeps the resting eyes and does its acting in motion.
    var expression: AvatarMood {
        switch self {
        case .speaking: return .idle
        case .idle, .thinking, .happy, .sad, .surprised: return self
        }
    }
}

/// Draws one parsed face, in viewBox units, once per frame. All geometry is prepared up front;
/// a frame only composes transforms.
struct AvatarFaceArt: View {
    let face: AvatarFaceRender
    let seed: String
    let mood: AvatarMood

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var fromMood: AvatarMood = .idle
    @State private var changedAt: TimeInterval = 0
    @State private var visible = true

    private var seedValue: UInt64 { Self.hash(seed) }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion || !visible)) { timeline in
            Canvas(opaque: false) { context, size in
                let now = timeline.date.timeIntervalSinceReferenceDate
                Self.draw(
                    in: context,
                    size: size,
                    face: face,
                    mood: mood,
                    from: fromMood,
                    sinceChange: max(0, now - changedAt),
                    time: now,
                    seed: seedValue,
                    animated: !reduceMotion
                )
            }
        }
        .onAppear { visible = true }
        .onDisappear { visible = false }
        .onChange(of: mood) { old, _ in
            fromMood = old
            changedAt = Date.timeIntervalSinceReferenceDate
        }
        .accessibilityHidden(true)
    }

    // MARK: Geometry

    private struct Motion {
        var body = CGAffineTransform.identity
        var glance = CGVector.zero
        var blink: CGFloat = 1
        var crossfade: Double = 1
    }

    private static func draw(
        in context: GraphicsContext,
        size: CGSize,
        face: AvatarFaceRender,
        mood: AvatarMood,
        from: AvatarMood,
        sinceChange: TimeInterval,
        time: TimeInterval,
        seed: UInt64,
        animated: Bool
    ) {
        let unit = min(size.width, size.height) / 100
        guard unit > 0 else { return }

        let motion = animated
            ? Self.motion(mood: mood, sinceChange: sinceChange, time: time, seed: seed)
            : Motion()

        var canvas = context
        canvas.translateBy(x: (size.width - 100 * unit) / 2, y: (size.height - 100 * unit) / 2)
        canvas.scaleBy(x: unit, y: unit)

        var bodyContext = canvas
        bodyContext.concatenate(motion.body)
        bodyContext.fill(face.body, with: .color(face.bodyFill))

        let target = face.eyes[mood.expression] ?? face.eyes[.idle] ?? []
        let source = animated ? (face.eyes[from.expression] ?? target) : []
        Self.drawEyes(source, in: canvas, alpha: 1 - motion.crossfade, motion: motion, face: face)
        Self.drawEyes(target, in: canvas, alpha: motion.crossfade, motion: motion, face: face)
    }

    private static func drawEyes(
        _ eyes: [AvatarFaceRender.Eye],
        in context: GraphicsContext,
        alpha: Double,
        motion: Motion,
        face: AvatarFaceRender
    ) {
        guard alpha > 0.01 else { return }
        for eye in eyes {
            var eyeContext = context
            eyeContext.concatenate(motion.body)
            eyeContext.translateBy(x: motion.glance.dx, y: motion.glance.dy)
            if motion.blink < 0.999 {
                eyeContext.translateBy(x: eye.center.x, y: eye.center.y)
                eyeContext.scaleBy(x: 1, y: motion.blink)
                eyeContext.translateBy(x: -eye.center.x, y: -eye.center.y)
            }
            eyeContext.opacity = alpha
            eyeContext.fill(eye.path, with: .color(face.eyeFill))
        }
    }

    private static func motion(mood: AvatarMood, sinceChange: TimeInterval, time: TimeInterval, seed: UInt64) -> Motion {
        var scale = CGSize(width: 1, height: 1)
        var offset = CGSize.zero
        var rotation: CGFloat = 0
        var glance = CGVector.zero

        let jitter = noise(seed, 1)
        let period: Double = mood == .speaking ? 1.8 + 1.2 * jitter : 3 + 3 * jitter
        let blinkLength = 0.13
        let local = (time + jitter * period).truncatingRemainder(dividingBy: period)
        var blink: CGFloat = 1
        if local < blinkLength {
            blink = 1 - 0.9 * CGFloat(sin(.pi * local / blinkLength))
        }

        switch mood {
        case .idle:
            let breath = 1.01 + 0.01 * sin(2 * .pi * time / 3.6)
            scale = CGSize(width: breath, height: breath)
        case .thinking:
            glance = CGVector(dx: 2 * sin(2 * .pi * time / 2.6), dy: 0)
            rotation = 0.026 * CGFloat(sin(2 * .pi * time / 4.4))
            offset = CGSize(width: 0, height: -0.4)
        case .speaking:
            offset = CGSize(width: 0, height: -0.9 * CGFloat(abs(sin(.pi * 2.5 * time))))
        case .happy:
            if sinceChange < 0.5 {
                offset = CGSize(width: 0, height: -4 * sin(.pi * sinceChange / 0.5))
            }
        case .sad:
            scale = CGSize(width: 1, height: 0.98)
            offset = CGSize(width: 0, height: 1.2)
        case .surprised:
            if sinceChange < 0.42 {
                let pop = 1 + 0.08 * sin(.pi * sinceChange / 0.42)
                scale = CGSize(width: pop, height: pop)
            }
        }

        let fade = sinceChange < 0.28 ? smooth(sinceChange / 0.28) : 1
        return Motion(
            body: transform(scale: scale, rotation: rotation, offset: offset),
            glance: glance,
            blink: blink,
            crossfade: fade
        )
    }

    private static func transform(scale: CGSize, rotation: CGFloat, offset: CGSize) -> CGAffineTransform {
        var transform = CGAffineTransform(translationX: 50 + offset.width, y: 50 + offset.height)
        transform = transform.rotated(by: rotation)
        transform = transform.scaledBy(x: scale.width, y: scale.height)
        transform = transform.translatedBy(x: -50, y: -50)
        return transform
    }

    private static func smooth(_ value: Double) -> Double {
        let clamped = min(max(value, 0), 1)
        return clamped * clamped * (3 - 2 * clamped)
    }

    /// Deterministic per-seed value in `0..<1` so a list does not blink in sync.
    private static func noise(_ seed: UInt64, _ salt: UInt64) -> Double {
        var value = seed ^ (salt &* 0x9E37_79B9_7F4A_7C15)
        value ^= value >> 30
        value = value &* 0xBF58_476D_1CE4_E5B9
        value ^= value >> 27
        value = value &* 0x94D0_49BB_1331_11EB
        value ^= value >> 31
        return Double(value % 10_000) / 10_000
    }

    private static func hash(_ text: String) -> UInt64 {
        var value: UInt64 = 0xCBF2_9CE4_8422_2325
        for byte in text.utf8 {
            value = (value ^ UInt64(byte)) &* 0x0000_0100_0000_01B3
        }
        return value
    }
}

// MARK: - Avatar

/// One Blobatar. Sized in points; rectangular sizes are allowed so group compositions stay square.
struct AvatarView: View {
    @EnvironmentObject private var store: TeamStore

    let seed: String?
    var width: CGFloat
    var height: CGFloat
    var identity: String = ""
    var mood: AvatarMood = .idle

    @State private var phase: Phase = .loading

    private enum Phase {
        case loading
        case face(AvatarFaceRender)
        case image(UIImage)
        case failed(String)
    }

    init(seed: String?, size: CGFloat, identity: String = "", mood: AvatarMood = .idle) {
        self.seed = seed
        self.width = size
        self.height = size
        self.identity = identity
        self.mood = mood
    }

    init(seed: String?, width: CGFloat, height: CGFloat, identity: String = "", mood: AvatarMood = .idle) {
        self.seed = seed
        self.width = width
        self.height = height
        self.identity = identity
        self.mood = mood
    }

    private var taskKey: String { "\(seed ?? "none")|\(store.baseURL.absoluteString)" }

    private var failureMessage: String? {
        if case .failed(let message) = phase { return message }
        return nil
    }

    var body: some View {
        ZStack {
            switch phase {
            case .loading:
                placeholder(loading: true)
            case .face(let face):
                AvatarFaceArt(face: face, seed: seed ?? "", mood: mood)
            case .image(let image):
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
            case .failed:
                placeholder(loading: false)
            }
        }
        .frame(width: width, height: height)
        .contentShape(Rectangle())
        .onTapGesture {
            guard failureMessage != nil else { return }
            Task { await load(force: true) }
        }
        .task(id: taskKey) { await load(force: false) }
        .accessibilityElement()
        .accessibilityHidden(failureMessage == nil)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(avatarAccessibilityLabel)
    }

    private var avatarAccessibilityLabel: String {
        let owner = identity.isEmpty ? "头像" : "\(identity)的头像"
        guard let failureMessage else { return owner }
        return "\(owner)加载失败：\(failureMessage)。点按重试。"
    }

    private func placeholder(loading: Bool) -> some View {
        Group {
            if loading {
                ProgressView()
                    .controlSize(.mini)
                    .tint(Palette.inkTertiary)
            } else {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: max(9, min(width, height) * 0.28), weight: .semibold))
                    .foregroundStyle(Palette.inkTertiary)
            }
        }
    }

    @MainActor
    private func load(force: Bool) async {
        let key = taskKey
        let baseURL = store.baseURL
        guard !Task.isCancelled else { return }
        guard let seed, !seed.isEmpty else {
            phase = .failed("没有头像标识")
            return
        }
        if !force {
            if let face = AvatarFaceLoader.shared.cached(baseURL: baseURL, seed: seed) {
                phase = .face(face)
                return
            }
            if let image = AvatarLoader.shared.cached(baseURL: baseURL, seed: seed) {
                phase = .image(image)
                return
            }
        }
        phase = .loading
        do {
            let face = try await AvatarFaceLoader.shared.render(baseURL: baseURL, seed: seed)
            guard !Task.isCancelled, taskKey == key else { return }
            phase = .face(face)
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled, taskKey == key else { return }
            // The layered face is the good path; the PNG keeps the avatar visible if it fails.
            do {
                let image = try await AvatarLoader.shared.image(baseURL: baseURL, seed: seed)
                guard !Task.isCancelled, taskKey == key else { return }
                phase = .image(image)
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled, taskKey == key else { return }
                phase = .failed(TeamStore.message(for: error))
            }
        }
    }
}

/// A group's face: member blobs overlapping, never a rounded tile. More than four
/// members keeps three faces and a `+N` for the ones that do not fit.
struct GroupAvatarView: View {
    let members: [AgentDTO]
    var size: CGFloat = 48

    private var overflow: Int { members.count > 4 ? members.count - 3 : 0 }

    private var shown: [AgentDTO] {
        Array(members.prefix(overflow > 0 ? 3 : 4))
    }

    private var slotCount: Int { shown.count + (overflow > 0 ? 1 : 0) }

    private var face: CGFloat { size * 0.62 }

    var body: some View {
        ZStack {
            ForEach(Array(shown.enumerated()), id: \.element.id) { index, member in
                AvatarView(seed: member.avatarSeed, size: face, identity: member.name)
                    .offset(Self.slot(index, count: slotCount, spread: size * 0.2))
            }
            if overflow > 0 {
                Text("+\(overflow)")
                    .font(.system(size: max(10, face * 0.28), weight: .bold))
                    .foregroundStyle(Palette.ink)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                    .frame(width: face * 0.72, height: face * 0.72)
                    .background(Circle().fill(Palette.ink.opacity(0.12)))
                    .offset(Self.slot(3, count: 4, spread: size * 0.2))
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private static func slot(_ index: Int, count: Int, spread: CGFloat) -> CGSize {
        let points: [CGSize]
        switch count {
        case 1:
            points = [.zero]
        case 2:
            points = [
                CGSize(width: -spread, height: spread * 0.25),
                CGSize(width: spread, height: -spread * 0.2),
            ]
        case 3:
            points = [
                CGSize(width: 0, height: -spread * 0.85),
                CGSize(width: -spread, height: spread * 0.7),
                CGSize(width: spread, height: spread * 0.7),
            ]
        default:
            points = [
                CGSize(width: spread * 0.95, height: -spread * 0.75),
                CGSize(width: -spread, height: spread * 0.15),
                CGSize(width: spread * 0.45, height: spread * 0.95),
                CGSize(width: -spread * 0.2, height: -spread * 0.95),
            ]
        }
        return points[min(index, points.count - 1)]
    }
}
