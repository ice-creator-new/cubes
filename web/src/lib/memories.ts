// Pure reading of the memory table: what still counts, which tab it falls on, how it sorts,
// who wrote it, and which messages a row came from. Kept out of the panel so it can be tested.

import type { Memory, MemoryScope } from "../api/types";
import { listTimestamp } from "./time";

export interface MemoryGroup {
  /** Null only for an agent-scope row whose bot is gone. */
  agentID: string | null;
  name: string;
  memories: Memory[];
}

/** Splits the table into what still counts and what the user may look back at. */
export function splitMemories(memories: Memory[]): { active: Memory[]; retired: Memory[] } {
  return {
    active: memories.filter((memory) => memory.status === "active"),
    retired: memories.filter((memory) => memory.status !== "active"),
  };
}

/** One tab's rows: a memory belongs to exactly one scope. */
export function memoriesInScope(memories: Memory[], scope: MemoryScope): Memory[] {
  return memories.filter((memory) => memory.scope === scope);
}

/** Content and topic, case-insensitive; an empty query keeps everything. */
export function searchMemories(memories: Memory[], query: string): Memory[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return memories;
  return memories.filter(
    (memory) =>
      memory.content.toLocaleLowerCase().includes(needle) || memory.topic.toLocaleLowerCase().includes(needle),
  );
}

function stamp(iso: string): number {
  const value = Date.parse(iso);
  return Number.isFinite(value) ? value : 0;
}

/** Pinned rows first, then newest by `created_at`; the input order breaks every tie. */
export function sortMemories(memories: Memory[]): Memory[] {
  return memories
    .map((memory, index) => ({ memory, index }))
    .sort((a, b) => {
      if (a.memory.pinned !== b.memory.pinned) return a.memory.pinned ? -1 : 1;
      const delta = stamp(b.memory.created_at) - stamp(a.memory.created_at);
      return delta !== 0 ? delta : a.index - b.index;
    })
    .map((entry) => entry.memory);
}

/**
 * The 成员 tab: one section per bot, named from the workspace directory, ordered by that name.
 * A row whose bot was deleted keeps a section of its own rather than vanishing.
 */
export function groupByAgent(memories: Memory[], names: Record<string, string>): MemoryGroup[] {
  const groups = new Map<string, MemoryGroup>();
  for (const memory of memories) {
    const key = memory.agent_id ?? "";
    let group = groups.get(key);
    if (!group) {
      group = {
        agentID: memory.agent_id,
        name: (memory.agent_id ? names[memory.agent_id] : undefined) ?? "已移除的成员",
        memories: [],
      };
      groups.set(key, group);
    }
    group.memories.push(memory);
  }
  return [...groups.values()].sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"));
}

/** Which finished replies wrote a memory; retired rows wrote nothing the user can still see. */
export function memoriesByMessage(memories: Memory[]): Map<string, Memory[]> {
  const map = new Map<string, Memory[]>();
  for (const memory of memories) {
    if (memory.status !== "active") continue;
    for (const id of memory.source_message_ids) {
      const list = map.get(id);
      if (list) list.push(memory);
      else map.set(id, [memory]);
    }
  }
  return map;
}

/** Why a retired row left the active set. */
export function retirementLabel(memory: Memory): string {
  return memory.superseded_by ? "被新的替换" : "已忘记";
}

/** Where a row came from, in the user's words. */
export function originLabel(memory: Memory, authorName: string | null | undefined): string {
  const name = `「${authorName ?? "机器人"}」`;
  if (memory.origin === "user") return "你添加的";
  if (memory.origin === "extract") return `${name}整理的`;
  return `${name}记下的`;
}

/** When a row counted, for the retired list: 「3月2日 – 3月9日」. */
export function memorySpan(memory: Memory, now = new Date()): string {
  const from = listTimestamp(memory.valid_from, now);
  const to = listTimestamp(memory.valid_to ?? memory.updated_at, now);
  if (!from && !to) return "";
  if (!to) return from;
  return `${from || "…"} – ${to}`;
}
