// Server-sent events as a value: the event stream is read with fetch (so the launch token can
// travel in a header) and every frame still arrives named and parsed, like EventSource did.

export interface SSEFrame {
  event: string;
  data: string;
}

/**
 * Incremental `text/event-stream` parser: feed it decoded chunks, get back the frames each
 * chunk completed. Comments (heartbeats) are dropped; an unnamed frame is `message`.
 */
export class SSEParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];

  push(chunk: string): SSEFrame[] {
    const frames: SSEFrame[] = [];
    this.buffer += chunk;
    for (let newline = this.buffer.indexOf("\n"); newline !== -1; newline = this.buffer.indexOf("\n")) {
      const line = this.buffer.slice(0, newline).replace(/\r$/, "");
      this.buffer = this.buffer.slice(newline + 1);
      if (line === "") {
        if (this.data.length) frames.push({ event: this.event || "message", data: this.data.join("\n") });
        this.event = "";
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") this.event = value;
      else if (field === "data") this.data.push(value);
    }
    return frames;
  }
}
