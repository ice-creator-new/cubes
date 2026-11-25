// 「设置 → 团队」: 主管验收, 忙时消息 and 团队简报, one section each. Reads from and writes back to
// the store, which answers the whole state; the stream then re-broadcasts it to every window.

import { useRef, useState } from "react";
import type { StepBudgetPreset, TeamSettings } from "../api/types";
import { FOLLOW_DEFAULT, modelGroups, modelOptionText, modelValue, parseModelValue } from "../lib/modelOptions";
import { useSegmentedThumb } from "../lib/motion";
import { SEND_MODES } from "../lib/sendMode";
import { supervisorName } from "../lib/team";
import { clockTime } from "../lib/time";
import { dispatchFailureWord } from "../lib/routeHint";
import { store, useTeam } from "../state/store";
import { Switch } from "./Connectors";

const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

// The three presets, shortest first; the numbers come from the backend so the standard
// preset shows the environment's own values.
const STEP_BUDGETS: { id: StepBudgetPreset; label: string; hint: string }[] = [
  { id: "unlimited", label: "不限", hint: "做到模型不再调用工具为止；随时可以点停止。" },
  { id: "standard", label: "标准", hint: "原来的节奏，够日常问答和一些小改动。" },
  { id: "long", label: "宽松", hint: "大多数要多查几步、多改几处的任务能一次做完。" },
  { id: "marathon", label: "很长", hint: "翻资料、改好几处代码这类长活，留给它更多步。" },
];

export function TeamSettingsSection() {
  const settings = useTeam((s) => s.teamSettings);
  const providers = useTeam((s) => s.providers);
  const supervisor = supervisorName(useTeam((s) => s.workspace));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ran, setRan] = useState(false);
  const segmented = useRef<HTMLDivElement>(null);
  const frequency = settings?.digest.frequency ?? "daily";
  const { thumb, ready } = useSegmentedThumb(segmented, frequency);
  // The send-mode pick has a thumb of its own: the three answers to a send that meets a reply.
  const modeGroup = useRef<HTMLDivElement>(null);
  const sendMode = settings?.send_mode ?? "steer";
  const { thumb: modeThumb, ready: modeReady } = useSegmentedThumb(modeGroup, sendMode);

  const patch = async (next: Partial<TeamSettings>) => {
    setSaving(true);
    setError(null);
    setRan(false);
    const failure = await store.setTeamSettings(next);
    setSaving(false);
    if (failure) setError(failure);
  };

  const runNow = async () => {
    setSaving(true);
    setError(null);
    setRan(false);
    const failure = await store.runDigestNow();
    setSaving(false);
    if (failure) setError(failure);
    else setRan(true);
  };

  if (!settings) {
    return (
      <section className="settings-section">
        <p className="hint">正在读取团队设置…</p>
      </section>
    );
  }

  const dispatchPick = settings.dispatch_model;
  const dispatchHealth = settings.dispatch_health;
  // One line in words, not a log: how many failed and why the newest one did.
  const dispatchWarning =
    dispatchHealth && dispatchHealth.failures_24h > 0
      ? `最近 24 小时分发失败 ${dispatchHealth.failures_24h} 次（最近一次：${dispatchFailureWord(dispatchHealth.last_error)}${
          dispatchHealth.last_at ? `，${clockTime(dispatchHealth.last_at)}` : ""
        }）`
      : null;
  const dispatchListed =
    dispatchPick === null ||
    providers === null ||
    Boolean(
      providers.providers
        .find((provider) => provider.id === dispatchPick.provider_id)
        ?.models.includes(dispatchPick.model),
    );

  return (
    <>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <section className="settings-section">
        <div className="settings-heading with-switch">
          <div className="settings-heading-text">
            <h4>主管验收</h4>
            <p>
              成员做完后先由{supervisor}对照目标检查，不合格就打回重做，通过了才交给你。会多花一些时间和用量。
            </p>
          </div>
          <Switch
            checked={settings.review.enabled}
            disabled={saving}
            label={settings.review.enabled ? "关掉主管验收" : "打开主管验收"}
            onChange={() => void patch({ review: { ...settings.review, enabled: !settings.review.enabled } })}
          />
        </div>

        {settings.review.enabled && (
          <label className="team-subfield">
            <span className="field-label">最多打回</span>
            <select
              className="field select"
              value={settings.review.max_rounds}
              disabled={saving}
              aria-label="最多打回"
              onChange={(event) =>
                void patch({ review: { ...settings.review, max_rounds: Number(event.target.value) } })
              }
            >
              {[1, 2, 3].map((rounds) => (
                <option key={rounds} value={rounds}>
                  {rounds} 次
                </option>
              ))}
            </select>
          </label>
        )}
      </section>

      <section className="settings-section">
        <div className="settings-heading">
          <h4>忙时消息</h4>
          <p>机器人正在回复时你发的消息怎么处理。成员可以各自换一种，没换的都用这里的。</p>
        </div>
        <div
          className={modeThumb ? "segmented has-thumb" : "segmented"}
          role="radiogroup"
          aria-label="机器人忙时你发的消息"
          ref={modeGroup}
        >
          {modeThumb && (
            <span
              className={modeReady ? "segmented-thumb" : "segmented-thumb instant"}
              aria-hidden="true"
              style={{ width: modeThumb.width, transform: `translateX(${modeThumb.x}px)` }}
            />
          )}
          {SEND_MODES.map((option) => (
            <button
              key={option.id}
              type="button"
              role="radio"
              data-seg={option.id}
              aria-checked={sendMode === option.id}
              className={sendMode === option.id ? "on" : ""}
              disabled={saving}
              onClick={() => void patch({ send_mode: option.id })}
            >
              {option.label}
            </button>
          ))}
        </div>
        <p className="hint">{SEND_MODES.find((option) => option.id === sendMode)?.hint}</p>
      </section>

      <section className="settings-section">
        <div className="settings-heading">
          <h4>每次能做多少步</h4>
          <p>
            机器人做长任务时，一轮发言能连续调用多少次工具。默认不设上限，做到完成为止；也可以选一个上限来控成本，用到上限会先停在这里，发「继续」接着做。
          </p>
        </div>
        <div className="step-budgets" role="radiogroup" aria-label="每次能做多少步">
          {STEP_BUDGETS.map((option) => {
            const on = option.id === settings.step_budget;
            const numbers = settings.step_budgets[option.id];
            return (
              <button
                key={option.id}
                type="button"
                role="radio"
                aria-checked={on}
                className={on ? "step-budget on" : "step-budget"}
                disabled={saving}
                onClick={() => void patch({ step_budget: option.id })}
              >
                <span className="step-budget-title">{option.label}</span>
                <span className="step-budget-numbers">
                  {numbers.unlimited ? (
                    <>
                      <span>不设步数上限</span>
                      <span>模型不再调用工具就算做完</span>
                    </>
                  ) : (
                    <>
                      <span>每轮最多 {numbers.turn_tool_calls} 次工具调用</span>
                      <span>整次任务最多 {numbers.run_tool_calls} 次</span>
                    </>
                  )}
                </span>
                <span className="step-budget-hint">{option.hint}</span>
              </button>
            );
          })}
        </div>
      </section>

      <section className="settings-section">
        <div className="settings-heading">
          <h4>分发用的模型</h4>
          <p>
            群里每条消息先由看不见的分发判断该谁回答、还是要并入或接着做哪件事。它不发言、不写东西，只做这一下判断；自动会用最快的那个模型。
          </p>
        </div>
        <label className="field-label" htmlFor="dispatch-model">
          分发用的模型
        </label>
        <select
          id="dispatch-model"
          className="field select"
          value={dispatchPick ? modelValue(dispatchPick) : FOLLOW_DEFAULT}
          disabled={saving}
          aria-label="分发用的模型"
          onChange={(event) => void patch({ dispatch_model: parseModelValue(event.target.value) })}
        >
          <option value={FOLLOW_DEFAULT}>自动（最快的）</option>
          {dispatchPick && !dispatchListed && (
            <option value={modelValue(dispatchPick)}>{dispatchPick.model}（已不可用）</option>
          )}
          {modelGroups(providers).map((group) => (
            <optgroup key={group.provider.id} label={group.provider.name}>
              {group.models.map((model) => (
                <option key={model} value={modelValue({ provider_id: group.provider.id, model })}>
                  {modelOptionText(group.provider, model)}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {/* Quiet, but never hidden: a dispatcher that keeps failing is why a group's messages
            land on whoever spoke last. */}
        {dispatchWarning && (
          <p className="dispatch-health" role="status">
            {dispatchWarning}
          </p>
        )}
      </section>

      <section className="settings-section">
        <div className="settings-heading">
          <h4>同时做几件</h4>
          <p>群里的成员各自开任务同时做，最多几件一起跑。多出来的先排队，前面的做完就自动开始；每个成员同一时间只做一件。</p>
        </div>
        <label className="team-subfield">
          <span className="field-label">同时运行的任务</span>
          <input
            className="field"
            type="number"
            min={1}
            max={8}
            step={1}
            value={settings.max_parallel_tasks}
            disabled={saving}
            aria-label="同时运行的任务数"
            onChange={(event) => {
              const value = Math.min(8, Math.max(1, Math.round(Number(event.target.value))));
              if (!Number.isFinite(value)) return;
              void patch({ max_parallel_tasks: value });
            }}
          />
        </label>
      </section>

      <section className="settings-section">
        <div className="settings-heading with-switch">
          <div className="settings-heading-text">
            <h4>团队简报</h4>
            <p>{supervisor}定时在私聊里发一段：做了什么、谁花了多少、卡在哪里。</p>
          </div>
          <Switch
            checked={settings.digest.enabled}
            disabled={saving}
            label={settings.digest.enabled ? "关掉团队简报" : "打开团队简报"}
            onChange={() => void patch({ digest: { ...settings.digest, enabled: !settings.digest.enabled } })}
          />
        </div>

        {settings.digest.enabled && (
          <div className="team-digest-fields">
            <div
              className={thumb ? "segmented has-thumb" : "segmented"}
              role="radiogroup"
              aria-label="简报频率"
              ref={segmented}
            >
              {thumb && (
                <span
                  className={ready ? "segmented-thumb" : "segmented-thumb instant"}
                  aria-hidden="true"
                  style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
                />
              )}
              {(
                [
                  ["daily", "每天"],
                  ["weekly", "每周"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  data-seg={value}
                  aria-checked={frequency === value}
                  className={frequency === value ? "on" : ""}
                  disabled={saving}
                  onClick={() => void patch({ digest: { ...settings.digest, frequency: value } })}
                >
                  {label}
                </button>
              ))}
            </div>

            {settings.digest.frequency === "weekly" && (
              <select
                className="field select"
                value={settings.digest.weekday}
                disabled={saving}
                aria-label="简报星期"
                onChange={(event) =>
                  void patch({ digest: { ...settings.digest, weekday: Number(event.target.value) } })
                }
              >
                {WEEKDAY_LABELS.map((label, index) => (
                  <option key={index} value={index}>
                    {label}
                  </option>
                ))}
              </select>
            )}

            <input
              type="time"
              className="field"
              value={settings.digest.time}
              disabled={saving}
              aria-label="简报时间"
              onChange={(event) => void patch({ digest: { ...settings.digest, time: event.target.value } })}
            />

            <button type="button" className="ghost-button" disabled={saving} onClick={() => void runNow()}>
              {saving ? "生成中…" : "现在生成一次"}
            </button>
          </div>
        )}

        {ran && <p className="hint">已开始，写好后会发到「{supervisor}」的私聊。</p>}
      </section>
    </>
  );
}
