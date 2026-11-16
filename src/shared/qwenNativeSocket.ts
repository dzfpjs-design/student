/** Main-owned provider socket; credentials and URLs never reach the renderer. */
export type QwenNativeSocketOpen = { nativeHostConnectionId: string; model: string; voice: string }
export type QwenNativeSocketEvent = { hostConnectionId: string; data?: string; closed?: true; error?: string }
