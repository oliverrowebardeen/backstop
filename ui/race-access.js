/** Live controls fail closed until the server explicitly enables them. */
export function raceControls({liveAvailable, active, hasKey, replayAvailable}) {
  return {
    showCredentials: liveAvailable === true,
    readOnlyQuestion: liveAvailable !== true,
    disableRun: active || liveAvailable !== true || !hasKey,
    disableReplay: active || !replayAvailable,
  };
}

export function liveHeaders(key) {
  const token = key.trim();
  if (!token) throw new Error('Enter your Backstop API key to run a live race.');
  return {'Content-Type': 'application/json', Authorization: `Bearer ${token}`};
}
