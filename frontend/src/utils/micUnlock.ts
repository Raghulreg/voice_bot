let sharedCtx: AudioContext | null = null

export function unlockMicrophone(): AudioContext {
  const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
  if (!sharedCtx || sharedCtx.state === 'closed') sharedCtx = new Ctx()
  void sharedCtx.resume()
  return sharedCtx
}

export function sharedAudioContext(): AudioContext {
  return unlockMicrophone()
}
