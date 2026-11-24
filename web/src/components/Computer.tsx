// 「电脑」panel, opened from the rail: which computer the bots drive (this Mac, a cloud
// computer, or either), the state of each machine, the macOS permissions this Mac needs, who is
// using the computer right now with a stop for that run, the newest screenshot, and the command
// that connects a cloud computer.

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import type { ComputerMachine, ComputerPermissions, ComputerState, ComputerTarget } from "../api/types";
import { openExternally } from "../lib/links";
import { useSegmentedThumb } from "../lib/motion";
import { store, useTeam } from "../state/store";
import { Icon } from "./Icon";
import { useAttachmentURL } from "./Images";
import { Dialog } from "./Panels";

const TARGETS: readonly { id: ComputerTarget; label: string; note: string }[] = [
  { id: "local", label: "本机", note: "机器人用你正在用的这台 Mac，不用另外运行什么。" },
  { id: "cloud", label: "云端", note: "机器人用另一台连到这个后端的电脑，比如一台服务器。" },
  { id: "mixed", label: "混合", note: "两台都能用，机器人按任务挑一台。" },
];

/** Run on the cloud computer, not this Mac. The backend must be reachable from there. */
export function cloudCommand(baseURL: string, token: string): string {
  const server = baseURL.replace(/\/+$/, "").replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  return [
    "python3 computer/cubes_computer.py",
    `--server ${server}`,
    `--token ${token}`,
    "--place cloud",
    "--name 云端电脑",
  ].join(" \\\n  ");
}

type Place = "local" | "cloud";

/** The machines a target can use, each with where it lives. An older payload has no
 *  per-machine split: its top-level fields describe the one machine in use. */
function machinesOf(computer: ComputerState, target: ComputerTarget): { place: Place; machine: ComputerMachine }[] {
  const places: Place[] = target === "mixed" ? ["local", "cloud"] : [target];
  if (computer.machines) return places.map((place) => ({ place, machine: computer.machines![place] }));
  const { connected, name, os, screen, view } = computer;
  return [{ place: target === "cloud" ? "cloud" : "local", machine: { connected, name, os, screen, view } }];
}

function statusOf(machine: ComputerMachine): string {
  if (!machine.connected) return "未连接";
  const screen = machine.screen ? `${machine.screen.width}×${machine.screen.height}` : null;
  return ["已连接", machine.os, screen].filter(Boolean).join(" · ");
}

const AUTH_GUIDE_MS = 90_000;

/** Screen recording and accessibility for this Mac, polled until both are granted. */
function Permissions({ onReady }: { onReady: () => void }) {
  const [perms, setPerms] = useState<ComputerPermissions | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let gone = false;
    const take = (next: ComputerPermissions) => {
      if (gone) return;
      setPerms(next);
      if (!next.needed || (next.screen && next.accessibility)) onReady();
    };
    const load = () => {
      void store.client.computerPermissions().then(take).catch((reason: unknown) => {
        if (!gone) setError(reason instanceof Error ? reason.message : "读不到授权状态。");
      });
    };
    load();
    const poll = window.setInterval(load, 2000);
    // If nothing was granted in time, the system's own permission page is the next step.
    const guide = window.setTimeout(() => {
      void store.client.requestComputerPermissions().then(take).catch(() => undefined);
    }, AUTH_GUIDE_MS);
    return () => {
      gone = true;
      window.clearInterval(poll);
      window.clearTimeout(guide);
    };
  }, [onReady]);

  const ask = () => {
    setAsking(true);
    setError(null);
    void store.client
      .requestComputerPermissions()
      .then((next) => {
        setPerms(next);
        if (!next.needed || (next.screen && next.accessibility)) onReady();
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "没有打开授权。"))
      .finally(() => setAsking(false));
  };

  const rows: [string, boolean | undefined][] = [
    ["屏幕录制", perms?.screen],
    ["辅助功能", perms?.accessibility],
  ];
  return (
    <div className="computer-perms">
      <p className="hint">要让机器人看屏幕、用键盘鼠标，需要在系统里授权。只用终端的话不需要。</p>
      <ul className="computer-perm-list">
        {rows.map(([label, ok]) => (
          <li key={label}>
            <span className={ok === undefined ? "connector-dot" : ok ? "connector-dot ok" : "connector-dot error"} aria-hidden="true" />
            {label}
            <span className={ok ? "computer-perm-state ok" : "computer-perm-state"}>
              {ok === undefined ? "检查中…" : ok ? "已授权" : "未授权"}
            </span>
          </li>
        ))}
      </ul>
      <div className="computer-perm-actions">
        <button type="button" className="primary-button" disabled={asking} onClick={ask}>
          {asking ? "正在请求…" : "授权"}
        </button>
        <p className="hint">系统没有弹窗的话，90 秒后会自动打开「系统设置 → 隐私与安全性」。</p>
      </div>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function MachineCard({
  place,
  machine,
  permissions,
}: {
  place: Place;
  machine: ComputerMachine;
  permissions?: ReactNode;
}) {
  return (
    <section className="provider-card computer-card">
      <div className="connector-head">
        <span className="connector-mark">
          <Icon name={place === "cloud" ? "globe" : "computer"} size={18} />
        </span>
        <span className="connector-head-text">
          <span className="connector-name">{machine.name ?? (place === "cloud" ? "云端电脑" : "这台 Mac")}</span>
          <span className="connector-status">
            <span className={machine.connected ? "connector-dot ok" : "connector-dot"} aria-hidden="true" />
            {statusOf(machine)}
          </span>
        </span>
        {machine.connected && machine.view && (
          <a className="connector-action" href={machine.view} target="_blank" rel="noreferrer" onClick={openExternally}>
            <Icon name="link" size={13} />
            实时画面
          </a>
        )}
      </div>
      {permissions}
    </section>
  );
}

function Preview({ id }: { id: string }) {
  const url = useAttachmentURL(id);
  return (
    <div className="computer-preview">
      {url ? <img src={url} alt="电脑的最新截图" draggable={false} /> : <span className="image-tile-wait" aria-hidden="true" />}
    </div>
  );
}

function CloudCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <section className="settings-section computer-pair">
      <div className="computer-pair-head">
        <span className="field-label">连接云端电脑</span>
        <button
          type="button"
          className="connector-action"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(command.replace(/ \\\n {2}/g, " "));
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1600);
            } catch {
              // No clipboard (an insecure context): the command stays selectable.
            }
          }}
        >
          <Icon name={copied ? "check" : "copy"} size={13} />
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="computer-command">{command}</pre>
      <p className="hint">在云端电脑的 Cubes 仓库目录里运行。它要能访问到这个后端；后端地址变了，重新复制一次。</p>
    </section>
  );
}

export function ComputerPanel({ onClose }: { onClose: () => void }) {
  const computer = useTeam((s) => s.computer);
  const computerError = useTeam((s) => s.computerError);
  const baseURL = useTeam((s) => s.baseURL);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [hostReady, setHostReady] = useState(false);
  const markHostReady = useCallback(() => setHostReady(true), []);
  const holder = computer?.holder ?? null;
  const target = computer?.target ?? "local";
  const segmented = useRef<HTMLDivElement>(null);
  const { thumb, ready } = useSegmentedThumb(segmented, target);

  // Only local mode waits on this Mac's permissions; switching away drops the check.
  useEffect(() => {
    setHostReady(target !== "local");
  }, [target]);

  const pick = (id: ComputerTarget) => {
    if (!computer || target === id) return;
    setSaving(true);
    setError(null);
    void store.setComputerTarget(id).then((failure) => {
      setSaving(false);
      if (failure) setError(failure);
    });
  };

  const stop = () => {
    setStopping(true);
    setError(null);
    void store.stopComputer().then((failure) => {
      setStopping(false);
      if (failure) setError(failure);
    });
  };

  return (
    <Dialog title="电脑" onClose={onClose} wide>
      <div className="settings computer">
        <section className="settings-section">
          <span className="field-label">机器人用哪台电脑</span>
          <div
            className={thumb ? "segmented has-thumb" : "segmented"}
            role="radiogroup"
            aria-label="机器人用哪台电脑"
            ref={segmented}
          >
            {thumb && (
              <span
                className={ready ? "segmented-thumb" : "segmented-thumb instant"}
                aria-hidden="true"
                style={{ width: thumb.width, transform: `translateX(${thumb.x}px)` }}
              />
            )}
            {TARGETS.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                role="radio"
                data-seg={id}
                aria-checked={target === id}
                className={target === id ? "on" : ""}
                disabled={!computer || saving}
                onClick={() => pick(id)}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="hint">
            {TARGETS.find((option) => option.id === target)?.note}每个任务第一次用电脑时会先问你。
          </p>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </section>

        {computerError && (
          <p className="inline-error" role="alert">
            {computerError}
            <button type="button" className="link-button" onClick={() => void store.loadComputer()}>
              重新读取
            </button>
          </p>
        )}
        {!computer && !computerError && <p className="hint">正在读取电脑状态…</p>}

        {computer && (
          <>
            <section className="settings-section">
              <span className="field-label">{target === "mixed" ? "两台电脑" : "电脑"}</span>
              <div className="computer-machines">
                {machinesOf(computer, target).map(({ place, machine }) => (
                  <MachineCard
                    key={place}
                    place={place}
                    machine={machine}
                    permissions={
                      place === "local" && target === "local" && !hostReady ? (
                        <Permissions onReady={markHostReady} />
                      ) : undefined
                    }
                  />
                ))}
              </div>
            </section>

            {(holder || computer.latest_image_id) && (
              <section className="settings-section">
                <span className="field-label">正在做什么</span>
                {holder && (
                  <div className="provider-card computer-holder">
                    <span className="connector-status">
                      <span className="connector-dot busy" aria-hidden="true" />「{holder.agent_name}」正在使用电脑
                    </span>
                    <button type="button" className="connector-action danger" disabled={stopping} onClick={stop}>
                      <Icon name="stop" size={13} />
                      {stopping ? "正在停止…" : "停止"}
                    </button>
                  </div>
                )}
                {computer.latest_image_id && <Preview id={computer.latest_image_id} />}
              </section>
            )}

            {computer.token && target !== "local" && <CloudCommand command={cloudCommand(baseURL, computer.token)} />}
          </>
        )}
      </div>
    </Dialog>
  );
}
