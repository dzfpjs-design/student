type MediaWindow = {
  isDestroyed(): boolean
  loadURL(url: string): Promise<unknown>
  webContents: { executeJavaScript(script: string): Promise<unknown> }
}

async function bounded<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([promise, new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('受控内容播放确认超时。')), timeoutMs)
    })])
  } finally { if (timer) clearTimeout(timer) }
}

export async function prepareControlledMediaPlayback(
  window: MediaWindow,
  sourceUrl: string,
  isCurrent: () => boolean,
  timeoutMs = 3500,
): Promise<void> {
  const requireCurrent = (): void => {
    if (window.isDestroyed() || !isCurrent()) throw new Error('受控内容窗口已变化，没有确认新播放。')
  }
  requireCurrent()
  // Chromium's direct media document can reject navigation while its video is
  // usable. Only exact-source playback below can qualify that exceptional case.
  try { await bounded(window.loadURL(sourceUrl), timeoutMs) } catch { /* Require media proof below. */ }
  requireCurrent()
  const state = await bounded(window.webContents.executeJavaScript(`(async () => {
    const video = document.querySelector('video');
    if (location.href !== ${JSON.stringify(sourceUrl)} || !video) return null;
    Object.assign(video.style, { width: '100vw', height: '100vh', maxWidth: '100vw', maxHeight: '100vh', objectFit: 'contain' });
    if (!video.dataset.hscControlledPlayback) {
      video.dataset.hscControlledPlayback = '1';
      video.addEventListener('ended', () => console.log('HSC_CONTROLLED_MEDIA_ENDED'), { once: true });
    }
    try { await video.play(); } catch { return null; }
    const initial = video.currentTime;
    await new Promise(resolve => setTimeout(resolve, 120));
    return { url: location.href, currentSrc: video.currentSrc, readyState: video.readyState,
      width: video.videoWidth, height: video.videoHeight, advanced: video.currentTime > initial,
      paused: video.paused, error: video.error ? video.error.code : null };
  })()`), timeoutMs)
  requireCurrent()
  const media = state as Record<string, unknown> | null
  if (!media || media.url !== sourceUrl || media.currentSrc !== sourceUrl ||
    typeof media.readyState !== 'number' || media.readyState < 2 ||
    typeof media.width !== 'number' || media.width <= 0 ||
    typeof media.height !== 'number' || media.height <= 0 ||
    media.advanced !== true || media.paused !== false || media.error !== null) {
    throw new Error('受控内容尚未真正开始播放，没有确认启动。')
  }
}
