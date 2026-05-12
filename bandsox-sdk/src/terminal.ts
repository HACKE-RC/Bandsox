/**
 * Terminal session over WebSocket for interactive PTY access to a VM.
 *
 * Protocol: server sends base64-encoded text frames (stdout).
 * Client sends JSON: {type:"input", data:"<base64>"} or {type:"resize", cols:N, rows:N}.
 */

export type TerminalOutputCallback = (data: string) => void;
export type TerminalCloseCallback = () => void;
export type TerminalErrorCallback = (err: Event) => void;

export class TerminalSession {
  private ws: WebSocket;
  private _closed = false;

  constructor(ws: WebSocket) {
    this.ws = ws;
  }

  get closed(): boolean {
    return this._closed;
  }

  onOutput(callback: TerminalOutputCallback): void {
    this.ws.addEventListener("message", (event: MessageEvent) => {
      callback(atob(event.data));
    });
  }

  onClose(callback: TerminalCloseCallback): void {
    this.ws.addEventListener("close", () => {
      this._closed = true;
      callback();
    });
  }

  onError(callback: TerminalErrorCallback): void {
    this.ws.addEventListener("error", callback);
  }

  sendInput(text: string): void {
    this.ws.send(JSON.stringify({ type: "input", data: btoa(text) }));
  }

  resize(cols: number, rows: number): void {
    this.ws.send(JSON.stringify({ type: "resize", cols, rows }));
  }

  close(): void {
    this._closed = true;
    this.ws.close();
  }
}
