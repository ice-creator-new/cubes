//
//  AvatarMood.swift
//  cubes
//
//  What a member is doing right now, as its face shows it.
//

import Foundation

enum AvatarMood: String, Equatable, Sendable {
    /// Waiting: breathes and blinks.
    case idle
    /// Mentioned or queued, or working before any text arrives.
    case thinking
    /// Its reply is streaming.
    case speaking
    /// Just finished a reply.
    case happy
    /// Stopped or failed mid-reply.
    case sad
    /// Just got pulled in.
    case surprised
}
