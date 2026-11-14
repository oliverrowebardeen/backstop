/**
 * Follow one run with native SSE reconnection and stop on a terminal event.
 * @param {string} runId
 * @param {{onEvent: (event: object) => void, onComplete: () => void,
 *   onFailure: (message: string) => void, onReconnect: () => void}} handlers
 * @param {typeof EventSource} Source
 * @returns {() => void} Stop viewing; the agent continues on the server.
 */
export function watchRun(runId, handlers, Source = EventSource) {
  const source = new Source(`/stream/${encodeURIComponent(runId)}`);
  let closed = false;
  const close = () => { closed = true; source.close(); };
  const fail = message => { close(); handlers.onFailure(message); };
  source.addEventListener('log', message => {
    if (closed) return;
    try { handlers.onEvent(JSON.parse(message.data)); }
    catch { fail('The run contains an invalid event.'); }
  });
  source.addEventListener('complete', () => {
    if (closed) return;
    close(); handlers.onComplete();
  });
  source.addEventListener('stream_error', message => {
    if (closed) return;
    let reason = 'The agent stopped before completion.';
    try {
      const data = JSON.parse(message.data);
      if (typeof data.message === 'string' && data.message.trim()) reason = data.message;
    } catch { reason = 'The server returned an invalid failure event.'; }
    fail(reason);
  });
  source.onerror = () => {
    if (closed) return;
    if (source.readyState === Source.CLOSED) fail('The run stream could not be opened. The agent may still be running.');
    else handlers.onReconnect();
  };
  return close;
}
