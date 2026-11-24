// A schedule's or a workflow's webhook: the secret URL an outside service POSTs to in order
// to run it now. The secret is shown exactly once — when it is created or rotated — so the
// panel keeps it on screen until the user says they copied it, and never asks for it again.

import { useEffect, useState } from "react";
import type { Webhook } from "../api/types";
import { errorMessage } from "../api/client";
import { listTimestamp } from "../lib/time";
import { store, useTeam } from "../state/store";
import { Icon } from "./Icon";

export function WebhookSection({
  targetKind,
  targetID,
}: {
  targetKind: "schedule" | "workflow";
  targetID: string;
}) {
  const baseURL = useTeam((s) => s.baseURL);
  // ``undefined`` while the first read is in flight, ``null`` when there is no hook yet.
  const [hook, setHook] = useState<Webhook | null | undefined>(undefined);
  // The one-time secret of the last create or rotate; nothing else can bring it back.
  const [secret, setSecret] = useState<string | null>(null);
  const [busy, setBusy] = useState<"create" | "rotate" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setHook(undefined);
    setSecret(null);
    setError(null);
    store.client
      .webhook(targetKind, targetID)
      .then((body) => {
        if (alive) setHook(body.webhook);
      })
      .catch((failure) => {
        if (!alive) return;
        setHook(null);
        setError(errorMessage(failure));
      });
    return () => {
      alive = false;
    };
  }, [targetKind, targetID]);

  const url = hook ? `${baseURL.replace(/\/$/, "")}${hook.path}` : "";

  const copy = (key: string, text: string) => {
    void navigator.clipboard.writeText(text).then(
      () => {
        setCopied(key);
        window.setTimeout(() => setCopied((current) => (current === key ? null : current)), 1600);
      },
      () => setError("没能写入剪贴板，请手动选中复制。"),
    );
  };

  const act = async (kind: "create" | "rotate" | "delete") => {
    if (busy) return;
    setBusy(kind);
    setError(null);
    try {
      if (kind === "delete") {
        if (!hook) return;
        await store.client.deleteWebhook(hook.id);
        setHook(null);
        setSecret(null);
      } else if (kind === "create") {
        const created = await store.client.createWebhook(targetKind, targetID);
        setHook(created);
        setSecret(created.secret);
      } else if (hook) {
        const rotated = await store.client.rotateWebhook(hook.id);
        setHook(rotated);
        setSecret(rotated.secret);
      }
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="webhook-section" aria-label="Webhook">
      <h4>
        <Icon name="webhook" size={14} />
        Webhook
      </h4>

      {hook === undefined && <p className="wfx-help">正在读取…</p>}

      {hook === null && (
        <>
          <p className="wfx-help">
            给它一个密钥网址：任何服务用 POST 调用这个网址，就立刻运行一次，请求体可以作为这次运行的输入。
          </p>
          <button
            type="button"
            className="ghost-button"
            disabled={busy !== null}
            onClick={() => void act("create")}
          >
            <Icon name="plus" size={14} />
            {busy === "create" ? "正在创建…" : "创建 Webhook"}
          </button>
        </>
      )}

      {hook && (
        <>
          <div className="webhook-line">
            <code className="webhook-url">{url}</code>
            <button
              type="button"
              className="icon-button"
              aria-label="复制网址"
              title={copied === "url" ? "已复制" : "复制网址"}
              onClick={() => copy("url", url)}
            >
              <Icon name={copied === "url" ? "check" : "copy"} size={14} />
            </button>
          </div>

          {secret ? (
            <div className="webhook-secret" role="status">
              <p className="webhook-secret-note">
                密钥只显示这一次，关掉就再也看不到了。现在就复制到调用方。
              </p>
              <div className="webhook-line">
                <code className="webhook-url">{secret}</code>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="复制密钥"
                  title={copied === "secret" ? "已复制" : "复制密钥"}
                  onClick={() => copy("secret", secret)}
                >
                  <Icon name={copied === "secret" ? "check" : "copy"} size={14} />
                </button>
              </div>
              <button
                type="button"
                className="ghost-button webhook-curl"
                onClick={() =>
                  copy(
                    "curl",
                    `curl -X POST '${url}' \\\n  -H 'Authorization: Bearer ${secret}' \\\n  -H 'Content-Type: application/json' \\\n  -d '{"input": "这次的输入"}'`,
                  )
                }
              >
                <Icon name={copied === "curl" ? "check" : "copy"} size={14} />
                {copied === "curl" ? "已复制 curl" : "复制 curl 示例"}
              </button>
            </div>
          ) : (
            <p className="wfx-help">
              密钥只在创建或轮换时显示一次。调用时带上
              <code> Authorization: Bearer &lt;密钥&gt;</code>，请求体可选，例如
              <code> {`{"input": "…"}`}</code>。
            </p>
          )}

          <p className="wfx-help">
            上次调用：{hook.last_called_at ? listTimestamp(hook.last_called_at) : "还没有被调用过"}
          </p>

          <div className="wfx-actions">
            <button
              type="button"
              className="ghost-button"
              disabled={busy !== null}
              onClick={() => void act("rotate")}
            >
              <Icon name="repeat" size={14} />
              {busy === "rotate" ? "正在轮换…" : "轮换密钥"}
            </button>
            <button
              type="button"
              className="ghost-button danger-ghost"
              disabled={busy !== null}
              onClick={() => void act("delete")}
            >
              <Icon name="trash" size={14} />
              {busy === "delete" ? "正在删除…" : "删除 Webhook"}
            </button>
          </div>
        </>
      )}

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
