import { readSse, SseParser, type SseMessage } from './sse';

const STREAM =
  ': keep-alive\r\n' +
  'event: thought\r\n' +
  'data: {"step": 1,\r\n' +
  'data:  "text": "two\\nlines"}\r\n' +
  '\r\n' +
  'event: empty\n\n' +
  'id: 7\nretry: 100\ndata: plain\n\n' +
  'event: answer\ndata: {"text": "ünïcödé €"}\n\n' +
  'event: done\ndata: {"unterminated": true}\n';

const EXPECTED: SseMessage[] = [
  { event: 'thought', data: '{"step": 1,\n "text": "two\\nlines"}' },
  { event: 'message', data: 'plain' },
  { event: 'answer', data: '{"text": "ünïcödé €"}' },
];

function parseInChunks(text: string, size: number): SseMessage[] {
  const parser = new SseParser();
  const out: SseMessage[] = [];
  for (let i = 0; i < text.length; i += size) out.push(...parser.push(text.slice(i, i + size)));
  return out;
}

function byteStream(bytes: Uint8Array, size: number): ReadableStream<Uint8Array> {
  let offset = 0;
  return new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) {
        controller.close();
        return;
      }
      controller.enqueue(bytes.slice(offset, offset + size));
      offset += size;
    },
  });
}

describe('server-sent event parsing', () => {
  it('joins multi-line data, skips comments, ids and empty or unterminated events', () => {
    expect(new SseParser().push(STREAM)).toEqual(EXPECTED);
  });

  it('gives the same events whatever the chunk boundaries, CRLF pairs included', () => {
    for (const size of [1, 2, 3, 5, 7, 13]) expect(parseInChunks(STREAM, size)).toEqual(EXPECTED);
    const parser = new SseParser();
    expect(parser.push('data: a\r')).toEqual([]);
    expect(parser.push('\n\r')).toEqual([{ event: 'message', data: 'a' }]);
    expect(parser.push('\ndata: b\n\n')).toEqual([{ event: 'message', data: 'b' }]);
  });

  it('decodes bytes split inside a multi-byte character', async () => {
    const bytes = new TextEncoder().encode(STREAM);
    const out: SseMessage[] = [];
    for await (const message of readSse(byteStream(bytes, 1))) out.push(message);
    expect(out).toEqual(EXPECTED);
  });

  it('cancels the body when the reader leaves early', async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: 1\n\ndata: 2\n\n'));
      },
      cancel,
    });
    for await (const message of readSse(body)) {
      expect(message.data).toBe('1');
      break;
    }
    expect(cancel).toHaveBeenCalled();
  });
});
