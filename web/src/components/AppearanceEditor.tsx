// The one appearance picker: silhouette, body colour and eye colour, with a live portrait.
// The member editor and the first-run screen both mount it, so a shape added to the catalogue
// reaches both places and neither keeps a second copy of the option sets.

import { useRef, useState } from "react";
import type { Appearance } from "../api/types";
import { COLORS, SHAPES, asHexColor, type ColorId, type HexColor, type ShapeId } from "../bot/catalog";
import { contrast } from "../bot/math";
import { fillOf, swatchOf } from "../bot/palette";
import { Avatar } from "./Avatar";

const SHAPE_LABELS: Record<ShapeId, string> = {
  round: "圆",
  blob: "团子",
  squircle: "方块",
  sparkle: "闪光",
  clover: "四叶",
  teardrop: "水滴",
  flower: "花",
  peak: "尖顶",
  capsule: "胶囊",
  arch: "拱门",
  pentagon: "五边形",
  star: "星星",
  hexagon: "六边形",
  pebble: "卵石",
  heart: "爱心",
  egg: "蛋",
  diamond: "菱形",
  octagon: "八边形",
  cat: "猫耳",
  bear: "熊耳",
  ghost: "幽灵",
};

const COLOR_LABELS: Record<ColorId, string> = {
  ink: "墨",
  cream: "奶油",
  clay: "陶土",
  coral: "珊瑚",
  tangerine: "橘",
  honey: "蜜",
  moss: "苔",
  jade: "玉",
  sky: "天蓝",
  iris: "鸢尾",
  plum: "梅",
  slate: "石板",
};

/**
 * A free colour: a quiet pill with the colour as a dot and a label, over a native colour
 * input, so clicking it opens the system picker. `on` marks it as the colour in use.
 */
export function ColorField({
  label,
  name,
  value,
  on,
  onChange,
}: {
  label: string;
  /** The accessible name of the input. */
  name: string;
  value: string;
  on: boolean;
  onChange: (hex: HexColor) => void;
}) {
  return (
    <label className={on ? "color-field on" : "color-field"}>
      <span className="color-field-dot" style={{ background: value }} aria-hidden="true" />
      <span>{label}</span>
      <input
        type="color"
        aria-label={name}
        value={value}
        onChange={(event) => {
          const hex = asHexColor(event.target.value);
          if (hex) onChange(hex);
        }}
      />
    </label>
  );
}

function ChoiceRow<T extends string>({
  label,
  labels,
  options,
  value,
  seed,
  size,
  swatch,
  grid,
  idleOnFocus,
  onPick,
  preview,
}: {
  label: string;
  labels: Record<T, string>;
  options: readonly T[];
  /** The current value; it may be outside `options` (a free colour), then nothing is checked. */
  value: string;
  seed: string;
  size: number;
  /** Lay the set out as a fixed swatch grid instead of a wrapping run. */
  swatch?: boolean;
  /** A wrapping run that breaks as a tidy block (fourteen shapes: two rows of seven). */
  grid?: boolean;
  /** Frozen frames that come alive while hovered or focused, so a silhouette can be judged. */
  idleOnFocus?: boolean;
  onPick: (option: T) => void;
  preview: (option: T) => Appearance;
}) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const [awake, setAwake] = useState<number | null>(null);
  const focusAt = (index: number) => {
    const count = options.length;
    const wrapped = ((index % count) + count) % count;
    const option = options[wrapped];
    if (!option) return;
    onPick(option);
    buttons.current[wrapped]?.focus();
  };
  return (
    <div className="appearance-choice">
      <div className="appearance-choice-label">{label}</div>
      <div
        className={`appearance-options${swatch ? " swatch" : ""}${grid ? " grid" : ""}`}
        role="radiogroup"
        aria-label={label}
        onKeyDown={(event) => {
          const index = buttons.current.findIndex((button) => button === document.activeElement);
          if (index < 0) return;
          if (event.key === "ArrowRight" || event.key === "ArrowDown") {
            event.preventDefault();
            focusAt(index + 1);
          } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
            event.preventDefault();
            focusAt(index - 1);
          } else if (event.key === "Home") {
            event.preventDefault();
            focusAt(0);
          } else if (event.key === "End") {
            event.preventDefault();
            focusAt(options.length - 1);
          }
        }}
      >
        {options.map((option, index) => (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={option === value}
            aria-label={`${label}：${labels[option]}`}
            title={labels[option]}
            tabIndex={option === value || (index === 0 && !(options as readonly string[]).includes(value)) ? 0 : -1}
            ref={(node) => {
              buttons.current[index] = node;
            }}
            className={option === value ? "appearance-option on" : "appearance-option"}
            onClick={() => onPick(option)}
            onPointerEnter={idleOnFocus ? () => setAwake(index) : undefined}
            onPointerLeave={idleOnFocus ? () => setAwake(null) : undefined}
            onFocus={idleOnFocus ? () => setAwake(index) : undefined}
            onBlur={idleOnFocus ? () => setAwake(null) : undefined}
          >
            <Avatar
              seed={seed}
              appearance={preview(option)}
              size={size}
              frozen
              track="off"
              awake={Boolean(idleOnFocus) && awake === index}
            />
          </button>
        ))}
      </div>
    </div>
  );
}

export function AppearanceEditor({
  seed,
  name,
  agentID,
  value,
  onChange,
  onReset,
  previewSize = 96,
  head = true,
}: {
  seed: string;
  /** The name the portrait answers under; the first-run screen updates it as the user types. */
  name: string;
  agentID?: string;
  value: Appearance;
  onChange: (look: Appearance) => void;
  /** Members can go back to the appearance their seed drew; the first run has no "default". */
  onReset?: () => void;
  /** The live portrait's size; the first-run screen gives it a little more room. */
  previewSize?: number;
  /** A sheet that names this block itself (a member's 形象 category) drops the 「外观」 row. */
  head?: boolean;
}) {
  const lookSwatch = swatchOf(value);
  const customBody = asHexColor(value.color);
  // A chosen eye that nearly matches the body disappears at sidebar size; say so, don't block.
  const faintEye = Boolean(value.eye) && contrast(lookSwatch.fill, lookSwatch.eyeFill) < 3;
  return (
    <>
      {head && (
        <div className="appearance-head">
          <span className="field-label">外观</span>
          {onReset && (
            <button type="button" className="link-button" onClick={onReset}>
              恢复默认
            </button>
          )}
        </div>
      )}
      <div className="appearance-grid">
        <div className="appearance-preview">
          <Avatar seed={seed} appearance={value} size={previewSize} name={name} agentID={agentID} />
        </div>
        <div className="appearance-choices">
          <ChoiceRow
            label="形状"
            labels={SHAPE_LABELS}
            options={SHAPES}
            value={value.shape}
            seed={seed}
            size={36}
            grid
            idleOnFocus
            onPick={(shape) => onChange({ ...value, shape })}
            preview={(shape) => ({ ...value, shape })}
          />
          <ChoiceRow
            label="颜色"
            labels={COLOR_LABELS}
            options={COLORS}
            value={value.color}
            seed={seed}
            size={36}
            swatch
            onPick={(color) => onChange({ ...value, color })}
            preview={(color) => ({ ...value, color })}
          />
          <div className="color-fields">
            <ColorField
              label={customBody ? customBody : "自定义"}
              name="自定义身体颜色"
              value={customBody ?? fillOf(value.color)}
              on={Boolean(customBody)}
              onChange={(color) => onChange({ ...value, color })}
            />
          </div>
          <div className="appearance-choice">
            <div className="appearance-choice-label" id="eye-color-label">
              眼睛
            </div>
            <div className="color-fields" role="group" aria-labelledby="eye-color-label">
              <button
                type="button"
                className={value.eye ? "color-field" : "color-field on"}
                aria-pressed={!value.eye}
                onClick={() => {
                  const { eye: _eye, ...rest } = value;
                  onChange(rest);
                }}
              >
                <span
                  className="color-field-dot"
                  style={{ background: swatchOf({ color: value.color }).eyeFill }}
                  aria-hidden="true"
                />
                <span>自动</span>
              </button>
              <ColorField
                label={value.eye ?? "自定义"}
                name="自定义眼睛颜色"
                value={lookSwatch.eyeFill}
                on={Boolean(value.eye)}
                onChange={(eye) => onChange({ ...value, eye })}
              />
            </div>
            {faintEye && <p className="hint">眼睛和身体的颜色太接近，在侧栏的小头像上可能看不清。</p>}
          </div>
        </div>
      </div>
    </>
  );
}
