declare module "@novnc/novnc" {
  export default class RFB {
    constructor(target: HTMLElement, urlOrChannel: string | WebSocket, options?: Record<string, unknown>);
    scaleViewport: boolean;
    resizeSession: boolean;
    showDotCursor: boolean;
    viewOnly: boolean;
    qualityLevel: number;
    compressionLevel: number;
    addEventListener(type: string, listener: (event: any) => void): void;
    removeEventListener(type: string, listener: (event: any) => void): void;
    disconnect(): void;
    sendCredentials(credentials: { username?: string; password?: string; target?: string }): void;
    sendKey(keysym: number, code: string, down?: boolean): void;
    sendCtrlAltDel(): void;
    focus(): void;
    blur(): void;
    machineShutdown(): void;
    machineReboot(): void;
    machineReset(): void;
    clipboardPasteFrom(text: string): void;
  }
}
