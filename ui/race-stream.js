/**
 * Read server-sent events from a fetch response body. EventSource cannot POST, and a live race
 * is one POST to /race/stream that starts both sides and streams their events on the same response.
 */

/** @returns {(chunk: string) => {event: string, data: any, id: string | null}[]} */
export function sseParser() {
  let buffer = '';
  return chunk => {
    buffer = (buffer + chunk).replace(/\r\n?/g, '\n');
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop();
    const messages = [];
    for (const block of blocks) {
      let event = 'message', id = null;
      const data = [];
      for (const line of block.split('\n')) {
        if (!line || line.startsWith(':')) continue;
        const colon = line.indexOf(':');
        const field = colon < 0 ? line : line.slice(0, colon);
        const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
        if (field === 'event') event = value;
        else if (field === 'data') data.push(value);
        else if (field === 'id') id = value;
      }
      if (!data.length) continue;
      const text = data.join('\n');
      let parsed;
      try { parsed = JSON.parse(text); } catch { throw new Error('The server sent an invalid event.'); }
      messages.push({event, data: parsed, id});
    }
    return messages;
  };
}

/**
 * @param {ReadableStream<Uint8Array>} body
 * @returns {AsyncGenerator<{event: string, data: any, id: string | null}>}
 */
export async function* readEvents(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parse = sseParser();
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      yield* parse(decoder.decode(value, {stream: true}));
    }
    yield* parse(decoder.decode() + '\n\n');
  } finally {
    reader.releaseLock();
  }
}
