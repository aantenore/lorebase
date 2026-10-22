/** One server-sent event: its name (`message` when the stream gave none) and its data text. */
export interface SseMessage {
  event: string;
  data: string;
}

/**
 * Incremental parser of a `text/event-stream` body (WHATWG HTML, "Server-sent events").
 * Chunks may split a line, a CRLF pair or a field anywhere; `push` keeps the unfinished tail.
 * Multi-line `data` fields are joined with a line feed; comments (`:`) and `id`/`retry` fields
 * are ignored; an event without data is dropped, and so is one the stream never closed.
 */
export class SseParser {
  private buffer = '';
  private event = '';
  private data: string[] = [];
  private pendingCr = false;

  push(chunk: string): SseMessage[] {
    let text = chunk;
    // A CR that ended the previous chunk already closed its line; skip the LF of the pair.
    if (this.pendingCr && text.startsWith('\n')) text = text.slice(1);
    this.pendingCr = false;
    this.buffer += text;
    const out: SseMessage[] = [];
    let start = 0;
    for (let i = 0; i < this.buffer.length; i++) {
      const char = this.buffer[i];
      if (char !== '\n' && char !== '\r') continue;
      this.line(this.buffer.slice(start, i), out);
      if (char === '\r') {
        if (i + 1 === this.buffer.length) this.pendingCr = true;
        else if (this.buffer[i + 1] === '\n') i++;
      }
      start = i + 1;
    }
    this.buffer = this.buffer.slice(start);
    return out;
  }

  private line(line: string, out: SseMessage[]): void {
    if (line === '') {
      if (this.data.length > 0) {
        out.push({ event: this.event === '' ? 'message' : this.event, data: this.data.join('\n') });
      }
      this.event = '';
      this.data = [];
      return;
    }
    if (line.startsWith(':')) return;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.event = value;
    else if (field === 'data') this.data.push(value);
  }
}

/** Reads the events of a response body as they arrive, until the body ends. */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  let finished = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        finished = true;
        yield* parser.push(decoder.decode());
        return;
      }
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
  } finally {
    // Leaving early (Stop, unmount) cancels the body, which closes the connection.
    if (!finished) reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
