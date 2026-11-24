// The event stream is read with fetch so the launch token can travel in a header; the parser
// and the client must keep EventSource's contract: the first fault closes and is reported once.

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import { SSEParser } from "./sse";

const BROKEN = "实时更新断了，状态可能已经变化。";
const UNPARSEABLE = "事件数据无法解析。请重新连接。";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

function sseResponse(chunks: string[]): Response {
  return new Response(streamOf(chunks), {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Collects what the stream reported and settles as soon as the stream ends by itself. */
function watch() {
  const events: [string, unknown][] = [];
  const { promise: closed, resolve } = Promise.withResolvers<string>();
  return {
    events,
    closed,
    onEvent: vi.fn((name: string, data: unknown) => {
      events.push([name, data]);
    }),
    onClose: vi.fn((message: string) => resolve(message)),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SSEParser", () => {
  it("returns nothing until a frame is terminated by a blank line", () => {
    const parser = new SSEParser();
    expect(parser.push("event: delta\n")).toEqual([]);
    expect(parser.push('data: {"a":1}\n')).toEqual([]);
    expect(parser.push("\n")).toEqual([{ event: "delta", data: '{"a":1}' }]);
  });

  it("splits several frames arriving in one chunk", () => {
    const parser = new SSEParser();
    const frames = parser.push(
      'event: run\ndata: {"id":"r1"}\n\nevent: delta\ndata: {"text":"你"}\n\nevent: done\ndata: {"id":"r1"}\n\n',
    );
    expect(frames).toEqual([
      { event: "run", data: '{"id":"r1"}' },
      { event: "delta", data: '{"text":"你"}' },
      { event: "done", data: '{"id":"r1"}' },
    ]);
  });

  it("reassembles a frame cut in the middle of its data", () => {
    const parser = new SSEParser();
    expect(parser.push('event: delta\ndata: {"text":"你')).toEqual([]);
    expect(parser.push('好"}\n\n')).toEqual([{ event: "delta", data: '{"text":"你好"}' }]);
  });

  it("ignores heartbeat comments and names an unnamed frame message", () => {
    const parser = new SSEParser();
    expect(parser.push(": ping\n\n")).toEqual([]);
    expect(parser.push(": ping\n")).toEqual([]);
    expect(parser.push("\ndata: 1\n\n")).toEqual([{ event: "message", data: "1" }]);
  });

  it("joins several data lines of one frame and accepts CRLF line endings", () => {
    const parser = new SSEParser();
    expect(parser.push("event: notes\r\ndata: a\r\ndata: b\r\n\r\n")).toEqual([
      { event: "notes", data: "a\nb" },
    ]);
  });

  it("tolerates an empty value and a line with no colon", () => {
    const parser = new SSEParser();
    expect(parser.push("data\n\n")).toEqual([{ event: "message", data: "" }]);
  });
});

describe("ApiClient events", () => {
  it("sends the launch token and parses every named event", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        'event: snapshot\ndata: {"workspace":{"agents":[]}}\n\n',
        ": ping\n\n",
        'event: delta\ndata: {"text":"好"}\n\n',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const stream = watch();

    new ApiClient("http://127.0.0.1:41234", "token-abc").events(stream);

    expect(await stream.closed).toBe(BROKEN);
    expect(stream.events).toEqual([
      ["snapshot", { workspace: { agents: [] } }],
      ["delta", { text: "好" }],
    ]);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer token-abc");
    expect(init.headers).toMatchObject({ Accept: "text/event-stream" });
    expect(stream.onClose).toHaveBeenCalledTimes(1);
  });

  it("closes on the first unparseable frame and never reconnects", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        'event: snapshot\ndata: {"ok":true}\n\n',
        "event: delta\ndata: {oops\n\n",
        "event: delta\ndata: {}\n\n",
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);
    const stream = watch();

    new ApiClient("http://127.0.0.1:41234").events(stream);

    expect(await stream.closed).toBe(UNPARSEABLE);
    expect(stream.onEvent).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reports a refused connection once", async () => {
    const fetchMock = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    vi.stubGlobal("fetch", fetchMock);
    const stream = watch();

    new ApiClient("http://127.0.0.1:41234").events(stream);

    expect(await stream.closed).toBe(BROKEN);
    expect(stream.onClose).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reports a stream the server ended instead of looking healthy", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([])));
    const stream = watch();
    new ApiClient("http://127.0.0.1:41234").events(stream);
    expect(await stream.closed).toBe(BROKEN);
  });

  it("says nothing when the caller closes the stream", async () => {
    const { promise: ready, resolve } = Promise.withResolvers<RequestInit>();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        resolve(init);
        return sseResponse(['event: snapshot\ndata: {}\n\n']);
      }),
    );
    const stream = watch();
    const stop = new ApiClient("http://127.0.0.1:41234").events(stream);
    const init = await ready;
    stop();
    expect((init.signal as AbortSignal).aborted).toBe(true);
    expect(stream.onClose).not.toHaveBeenCalled();
  });
});

describe("ApiClient requests", () => {
  it("carries the token on plain requests and leaves it off without one", async () => {
    const fetchMock = vi.fn(async () => new Response('{"status":"ok"}', { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await new ApiClient("http://127.0.0.1:41234", "token-abc").health();
    const [, withToken] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((withToken.headers as Record<string, string>).Authorization).toBe("Bearer token-abc");

    await new ApiClient("http://127.0.0.1:41234").health();
    const [, without] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect((without.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});
