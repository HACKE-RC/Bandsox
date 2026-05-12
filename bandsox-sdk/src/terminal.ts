/**
 * Terminal session over WebSocket for interactive PTY access to a VM.
 *
 * Protocol: server sends base64-encoded text frames (stdout).
 * Client sends JSON: {type:"input", data:"<base64>"} or {type:"resize", cols:N, rows:N}.
 */

export type TerminalOutputCallback = (data: string) => void;
export type TerminalCloseCallback = () => void;
export type TerminalErrorCallback = (err: Event) => void;

function bytesToBase64Text(data: ArrayBuffer | ArrayBufferView): string {
  const bytes =
    data instanceof ArrayBuffer
      ? new Uint8Array(data)
      : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  return new TextDecoder().decode(bytes);
}

function base64ToUtf8(data: string): string {
  const bytes = Uint8Array.from(atob(data), (char) => char.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

function utf8ToBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function decodeBase64Message(data: unknown): string | Promise<string> {
  if (typeof data === "string") {
    return base64ToUtf8(data);
  }

  if (data instanceof ArrayBuffer) {
    return base64ToUtf8(bytesToBase64Text(data));
  }

  if (ArrayBuffer.isView(data)) {
    return base64ToUtf8(bytesToBase64Text(data));
  }

  if (typeof Blob !== "undefined" && data instanceof Blob) {
    return data.text().then(base64ToUtf8);
  }

  throw new TypeError(
    `Unsupported terminal message data type: ${Object.prototype.toString.call(data)}`
  );
}

export class TerminalSession {
  private ws: WebSocket;
  private _closed = false;
  private outputCallback: TerminalOutputCallback | null = null;
  private closeCallback: TerminalCloseCallback | null = null;
  private errorCallback: TerminalErrorCallback | null = null;

  constructor(ws: WebSocket) {
    this.ws = ws;
    this.ws.addEventListener("message", (event) => {
      const callback = this.outputCallback;
      if (!callback) {
        return;
      }
      const decoded = decodeBase64Message(event.data);
      if (typeof decoded === "string") {
        callback(decoded);
      } else {
        void decoded.then(callback);
      }
    });
    this.ws.addEventListener("close", () => {
      this._closed = true;
      this.closeCallback?.();
    });
    this.ws.addEventListener("error", (event) => {
      this.errorCallback?.(event);
    });
  }

  get closed(): boolean {
    return this._closed;
  }

  onOutput(callback: TerminalOutputCallback): void {
    this.outputCallback = callback;
  }

  onClose(callback: TerminalCloseCallback): void {
    this.closeCallback = callback;
  }

  onError(callback: TerminalErrorCallback): void {
    this.errorCallback = callback;
  }

  sendInput(text: string): void {
    this.ws.send(JSON.stringify({ type: "input", data: utf8ToBase64(text) }));
  }

  resize(cols: number, rows: number): void {
    this.ws.send(JSON.stringify({ type: "resize", cols, rows }));
  }

  close(): void {
    this._closed = true;
    this.ws.close();
  }
}
