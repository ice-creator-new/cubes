// The specks a reaction throws. Every family has its own launch: hearts rise and sway, confetti
// falls and spins, sparks fly out fast and die quick, food drops, faces burst evenly. Each speck
// also carries a depth, which decides how large and how sharp it is — near specks are big and
// crisp, far ones small and soft — so a burst reads as a cloud rather than a flat ring.
//
// Motion is closed-form in time (a launch velocity, gravity and linear drag, solved rather than
// integrated), so `sample` is a pure function of `t`: a burst replays identically, a test can pin
// its path, and the renderer carries no state between frames.
//
// The layout is deterministic per (family, seed): a re-render never reshuffles a burst.

import { TAU } from "../bot/math";
import { EMOJI_GROUPS, type EmojiGroupId } from "./emoji";

export type ReactionMotion = "float" | "fall" | "radial" | "pop" | "drift";

export type ReactionFamily =
  | "hearts"
  | "confetti"
  | "sparks"
  | "faces"
  | "food"
  | "animals"
  | "marks";

/** How a speck is drawn. Emoji keep their own colours; a glyph takes `tone`. */
export type SpeckLook = "emoji" | "glyph" | "bar" | "dot";

/** One kind of speck in a family's mix; `weight` decides how many of the burst it takes. */
interface Kind {
  look: SpeckLook;
  /** Picked from per speck (emoji or a text glyph); empty for the two shapes. */
  glyphs: readonly string[];
  /** A colour for glyphs and shapes; empty means the accent (bars alternate theme tones). */
  tone: string;
  weight: number;
  /** Size against the family's range: dots ride smaller than emoji so a burst stays a cloud. */
  scale: number;
}

/** Launch and flight, in px and seconds. */
interface Physics {
  /** Where the cone points, radians: −π/2 is straight up on screen. */
  angle: number;
  /** How wide the cone is, radians; 2π throws in every direction. */
  spread: number;
  speed: readonly [number, number];
  /** px/s², positive is down. Negative lifts (hearts). */
  gravity: number;
  /** 1/s: how strongly the air slows it. Distance travelled caps near speed / drag. */
  drag: number;
  /** Degrees per second, signed. */
  spin: readonly [number, number];
  life: readonly [number, number];
  /** Sideways drift for floaters, px. */
  sway: number;
  /** Base size, px, before depth. */
  size: readonly [number, number];
}

export interface FamilyStyle {
  motion: ReactionMotion;
  count: number;
  kinds: readonly Kind[];
  physics: Physics;
}

const HEARTS = ["❤️", "💗", "💕", "💖"] as const;
const CONFETTI = ["🎊", "✨"] as const;
const SPARKS = ["✨", "⚡"] as const;
const FACES = ["😄", "😂", "🥹"] as const;
const FOOD = ["🍰", "🍪", "🍓"] as const;
const ANIMALS = ["🐾"] as const;
const MARKS = ["✦", "·"] as const;

/** A kind of emoji; a kind of glyph; a kind of shape. */
const emoji = (glyphs: readonly string[], weight: number, scale = 1): Kind => ({ look: "emoji", glyphs, tone: "", weight, scale });
const glyph = (glyphs: readonly string[], tone: string, weight: number, scale = 1): Kind => ({ look: "glyph", glyphs, tone, weight, scale });
const bar = (weight: number, scale = 0.8): Kind => ({ look: "bar", glyphs: [], tone: "", weight, scale });
const dot = (tone: string, weight: number, scale = 0.55): Kind => ({ look: "dot", glyphs: [], tone, weight, scale });

export const FAMILY_STYLE: Record<ReactionFamily, FamilyStyle> = {
  // Rising hearts, drifting as they go.
  hearts: {
    motion: "float",
    count: 12,
    kinds: [emoji(HEARTS, 7), glyph(["♥"], "#ff5d8f", 4, 0.85), dot("#ff8fae", 2, 0.5)],
    physics: { angle: -Math.PI / 2, spread: 2, speed: [70, 150], gravity: -30, drag: 1.2, spin: [-30, 30], life: [0.6, 0.85], sway: 8, size: [10, 16] },
  },
  // A fan that falls and tumbles, the theme's colours and a couple of party emoji.
  confetti: {
    motion: "fall",
    count: 18,
    kinds: [bar(8), emoji(CONFETTI, 5)],
    physics: { angle: Math.PI / 2, spread: 2.6, speed: [40, 110], gravity: 300, drag: 2.8, spin: [180, 520], life: [0.7, 0.9], sway: 0, size: [6, 11] },
  },
  // Sparks: fast, then gone. Drag makes them decelerate hard instead of drifting away.
  sparks: {
    motion: "radial",
    count: 16,
    kinds: [glyph(["✦", "✧"], "#ffce4d", 6, 0.8), emoji(SPARKS, 5), dot("#ffe9a8", 3, 0.5)],
    physics: { angle: 0, spread: TAU, speed: [200, 380], gravity: 30, drag: 3.2, spin: [0, 0], life: [0.4, 0.6], sway: 0, size: [7, 13] },
  },
  // Faces: a round pop with a little weight behind it.
  faces: {
    motion: "pop",
    count: 10,
    kinds: [emoji(FACES, 4), dot("#ffd479", 6, 0.5)],
    physics: { angle: 0, spread: TAU, speed: [90, 190], gravity: 140, drag: 2.2, spin: [-90, 90], life: [0.5, 0.72], sway: 0, size: [9, 14] },
  },
  // Food drops: crumbs and a little of what it was.
  food: {
    motion: "drift",
    count: 12,
    kinds: [emoji(FOOD, 5), dot("#e0a458", 6, 0.5), dot("#c98b4b", 3, 0.42)],
    physics: { angle: Math.PI / 2, spread: 2.4, speed: [60, 150], gravity: 300, drag: 2.6, spin: [-140, 140], life: [0.6, 0.82], sway: 0, size: [7, 12] },
  },
  // Paw prints padding out.
  animals: {
    motion: "pop",
    count: 10,
    kinds: [emoji(ANIMALS, 5), dot("#8fd0a0", 6, 0.5)],
    physics: { angle: 0, spread: TAU, speed: [80, 170], gravity: 90, drag: 2.4, spin: [-60, 60], life: [0.5, 0.72], sway: 0, size: [8, 13] },
  },
  // Anything without a family: a small even sparkle.
  marks: {
    motion: "radial",
    count: 10,
    kinds: [dot("", 8, 0.5), glyph(MARKS, "", 3, 0.8)],
    physics: { angle: 0, spread: TAU, speed: [110, 230], gravity: 20, drag: 2.8, spin: [0, 0], life: [0.45, 0.65], sway: 0, size: [6, 10] },
  },
};

// A burst follows the emoji's own category: the picker shows Unicode's groups, and each group
// throws the specks that suit it. A few glyphs deliberately cross over (a heart is a heart even
// though Unicode files it under Symbols; sparkles sparkle though they sit with Activities).
const GROUP_FAMILY: Record<EmojiGroupId, ReactionFamily> = {
  smileys: "faces",
  people: "sparks",
  animals: "animals",
  food: "food",
  travel: "sparks",
  activities: "confetti",
  objects: "marks",
  symbols: "marks",
  flags: "marks",
};

const HEART_GLYPHS = "❤🧡💛💚💙💜🤍🖤🩷🩵🩶💖💕💗💓💞💘💝";
const SPARK_OVER = "✨⚡🔥";
const CONFETTI_OVER = "🎉🎊🥳🎈";

/** The family a base glyph takes, from the catalogue's groups; the first group wins. */
const BY_GLYPH: Map<string, ReactionFamily> = (() => {
  const map = new Map<string, ReactionFamily>();
  for (const group of EMOJI_GROUPS) {
    for (const entry of group.entries) {
      const base = [...entry.emoji][0];
      if (base && !map.has(base)) map.set(base, GROUP_FAMILY[group.id]);
    }
  }
  for (const glyph of HEART_GLYPHS) map.set(glyph, "hearts");
  for (const glyph of SPARK_OVER) map.set(glyph, "sparks");
  for (const glyph of CONFETTI_OVER) map.set(glyph, "confetti");
  // Text glyphs that are not emoji, and so are in no catalogue group.
  map.set("♥", "hearts");
  map.set("✦", "sparks");
  map.set("✧", "sparks");
  return map;
})();

/**
 * The family an emoji belongs to. Variation selectors, skin tones and joined sequences are
 * dropped first, so `❤️` and `👍🏽` still find their family; anything unknown gets `marks`.
 */
export function reactionFamily(emoji: string): ReactionFamily {
  // Any single glyph of the sequence can name the family, so a joined or skinned form resolves
  // through its base: 👍🏽 is a 👍, 👨‍👩‍👧 is a 👨.
  const base = [...emoji].find((glyph) => BY_GLYPH.has(glyph));
  return base ? BY_GLYPH.get(base)! : "marks";
}

export interface Speck {
  look: SpeckLook;
  /** The emoji or glyph to draw; empty for a bar or a dot. */
  glyph: string;
  /** A colour for glyphs and shapes; empty takes the accent. */
  tone: string;
  /** Which of the theme's colours a bar takes (0–3). */
  hue: number;
  /** Start offset from the burst's origin, px: it leaves the pill's edge, not its centre. */
  x0: number;
  y0: number;
  /** Launch velocity, px/s. */
  vx: number;
  vy: number;
  gravity: number;
  drag: number;
  /** Spin at launch and the rate it gains, degrees and degrees a second. */
  spin0: number;
  spin: number;
  life: number;
  /** Seconds before it starts, so a burst does not fire as one flat ring. */
  delay: number;
  /** Sideways drift for floaters, px, and the phase it starts at. */
  sway: number;
  phase: number;
  /** 0.45 (far: small, soft) to 1 (near: full size, sharp). */
  z: number;
  /** Size at depth 1, px. */
  size: number;
}

/** A stable 0–1 number from two integers, so a layout is the same on every render. */
function noise(index: number, salt: number): number {
  const value = Math.sin(index * 127.1 + salt * 311.7) * 43758.5453;
  return value - Math.floor(value);
}

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** The kind a speck takes, by weight, from the burst's seed. */
function pickKind(kinds: readonly Kind[], draw: number): Kind {
  const total = kinds.reduce((sum, kind) => sum + kind.weight, 0);
  let edge = draw * total;
  for (const kind of kinds) {
    edge -= kind.weight;
    if (edge <= 0) return kind;
  }
  return kinds[kinds.length - 1]!;
}

/** The specks one burst throws, laid out by the family's launch. */
export function burst(family: ReactionFamily, seed = 0): Speck[] {
  const style = FAMILY_STYLE[family];
  const { physics } = style;
  return Array.from({ length: style.count }, (_, index) => {
    const roll = noise(index, seed);
    const kind = pickKind(style.kinds, noise(index, seed + 1));
    const glyphText = kind.glyphs.length ? kind.glyphs[Math.floor(noise(index, seed + 2) * kind.glyphs.length)]! : "";
    const angle = physics.angle + (noise(index, seed + 3) - 0.5) * physics.spread;
    // √ of the draw pushes specks toward the far end of the range: a burst should read as a
    // cloud with a wide edge, not as a knot at the centre.
    const speed = lerp(physics.speed[0], physics.speed[1], Math.sqrt(noise(index, seed + 4)));
    const drag = physics.drag * (0.82 + noise(index, seed + 13) * 0.36);
    const z = 0.45 + noise(index, seed + 5) * 0.55;
    const along = 4 + noise(index, seed + 6) * 7;
    return {
      look: kind.look,
      glyph: glyphText,
      tone: kind.tone,
      hue: Math.floor(noise(index, seed + 7) * 4),
      x0: Math.round(Math.cos(angle) * along * 10) / 10,
      y0: Math.round(Math.sin(angle) * along * 10) / 10,
      vx: Math.cos(angle) * speed,
      vy: Math.sin(angle) * speed,
      gravity: physics.gravity,
      drag,
      spin0: (roll - 0.5) * 12,
      spin: lerp(physics.spin[0], physics.spin[1], noise(index, seed + 8)),
      life: lerp(physics.life[0], physics.life[1], noise(index, seed + 9)),
      delay: Math.round(noise(index, seed + 10) * 70) / 1000,
      sway: physics.sway,
      phase: noise(index, seed + 12) * TAU,
      z,
      size: lerp(physics.size[0], physics.size[1], noise(index, seed + 11)) * kind.scale,
    };
  });
}

/** How long a burst runs, seconds, from the first launch to the last speck's end. */
export function burstLife(specks: readonly Speck[]): number {
  return specks.reduce((end, speck) => Math.max(end, speck.delay + speck.life), 0);
}

export interface SpeckFrame {
  x: number;
  y: number;
  /** Degrees. */
  rot: number;
  scale: number;
  opacity: number;
}

const smoothstep = (edge0: number, edge1: number, value: number) => {
  const t = Math.min(1, Math.max(0, (value - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
};

/**
 * Where one speck is at `t` seconds after the burst, relative to its origin. A launch velocity
 * with gravity and linear drag, solved: x = vx/k·(1−e^{−kt}) and y = (vy−g/k)/k·(1−e^{−kt}) + g/k·t.
 * Hearts also sway sideways as they rise.
 */
export function sample(speck: Speck, t: number): SpeckFrame {
  const age = t - speck.delay;
  if (age <= 0) return { x: speck.x0, y: speck.y0, rot: speck.spin0, scale: 0, opacity: 0 };
  const u = Math.min(1, age / speck.life);
  const seconds = u * speck.life;
  const k = speck.drag;
  let x: number;
  let y: number;
  if (k > 0.01) {
    const settle = 1 - Math.exp(-k * seconds);
    x = (speck.vx / k) * settle;
    y = ((speck.vy - speck.gravity / k) / k) * settle + (speck.gravity / k) * seconds;
  } else {
    x = speck.vx * seconds;
    y = speck.vy * seconds + 0.5 * speck.gravity * seconds * seconds;
  }
  if (speck.sway) x += speck.sway * Math.sin(seconds * 6.5 + speck.phase) * u;
  // In fast, hold, then out over the last third; the swell happens as it leaves the pill.
  const opacity = Math.min(1, age / 0.05) * (1 - smoothstep(0.62, 1, u));
  const scale = (0.5 + 0.5 * (1 - Math.exp(-age * 26))) * (1 - 0.22 * smoothstep(0.62, 1, u));
  return {
    x: speck.x0 + x,
    y: speck.y0 + y,
    rot: speck.spin0 + speck.spin * seconds,
    scale,
    opacity,
  };
}
