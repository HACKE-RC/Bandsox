/**
 * Terminal session over WebSocket for interactive PTY access to a VM.
 *
 * Protocol: server sends base64-encoded text frames (stdout).
 * Client sends JSON: {type:"input", data:"<base64>"} or {type:"resize", cols:N, rows:N}.
 */

export type TerminalOutputCallback = (data: string) => void;
export type TerminalCloseCallback = () => void;
export type TerminalErrorCallback = (err: Event | Error) => void;

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

function normalizeTerminalError(err: unknown): Event | Error {
  if (err instanceof Error) {
    return err;
  }
  if (typeof Event !== "undefined" && err instanceof Event) {
    return err;
  }
  return new Error(String(err));
}

export class TerminalSession {
  private ws: WebSocket;
  private _closed = false;
  private closeNotified = false;
  private outputCallback: TerminalOutputCallback | null = null;
  private closeCallback: TerminalCloseCallback | null = null;
  private errorCallback: TerminalErrorCallback | null = null;
  private pendingOutput: string[] = [];
  private pendingErrors: Array<Event | Error> = [];
  private pendingClose = false;

  constructor(ws: WebSocket) {
    this.ws = ws;
    this.ws.addEventListener("message", (event) => {
      let decoded: string | Promise<string>;
      try {
        decoded = decodeBase64Message(event.data);
      } catch (err) {
        this.emitError(normalizeTerminalError(err));
        return;
      }
      if (typeof decoded === "string") {
        this.emitOutput(decoded);
      } else {
        void decoded.then(
          (data) => this.emitOutput(data),
          (err) => this.emitError(normalizeTerminalError(err))
        );
      }
    });
    this.ws.addEventListener("close", () => {
      this.emitClose();
    });
    this.ws.addEventListener("error", (event) => {
      this.emitError(event);
    });
  }

  get closed(): boolean {
    return this._closed;
  }

  onOutput(callback: TerminalOutputCallback): void {
    this.outputCallback = callback;
    const pending = this.pendingOutput.splice(0);
    for (const data of pending) {
      callback(data);
    }
  }

  onClose(callback: TerminalCloseCallback): void {
    this.closeCallback = callback;
    if (this.pendingClose) {
      this.pendingClose = false;
      callback();
    }
  }

  onError(callback: TerminalErrorCallback): void {
    this.errorCallback = callback;
    const pending = this.pendingErrors.splice(0);
    for (const err of pending) {
      callback(err);
    }
  }

  sendInput(text: string): void {
    this.ws.send(JSON.stringify({ type: "input", data: utf8ToBase64(text) }));
  }

  resize(cols: number, rows: number): void {
    this.ws.send(JSON.stringify({ type: "resize", cols, rows }));
  }

  close(): void {
    this.emitClose();
    this.ws.close();
  }

  private emitOutput(data: string): void {
    const callback = this.outputCallback;
    if (callback) {
      callback(data);
    } else {
      this.pendingOutput.push(data);
    }
  }

  private emitError(err: Event | Error): void {
    const callback = this.errorCallback;
    if (callback) {
      callback(err);
    } else {
      this.pendingErrors.push(err);
    }
  }

  private emitClose(): void {
    if (this.closeNotified) {
      return;
    }
    this.closeNotified = true;
    this._closed = true;
    const callback = this.closeCallback;
    if (callback) {
      callback();
    } else {
      this.pendingClose = true;
    }
  }
}
