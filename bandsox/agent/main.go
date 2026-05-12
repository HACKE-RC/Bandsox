// BandSox guest agent — Go rewrite.
//
// Runs inside the microVM on the serial console (ttyS0).
// Reads JSON commands from stdin, writes JSON events to stdout.
//
// Key improvements over the Python agent:
//   - Static binary (~2.5 MB), zero runtime dependencies
//   - Instant startup, no interpreter overhead
//   - Goroutines instead of Python threads (lower memory, faster context switch)
//   - Vsock fast path for BOTH read_file and write_file
//   - No artificial serial throttling delays
//   - Raw file reads that the host can format with offset/limit/footer/header
//
// Build: CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o agent .

package main

import (
	"bufio"
	"bytes"
	"crypto/md5"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/creack/pty"
)

// =============================================================================
// Protocol types
// =============================================================================

type Request struct {
	Type     string            `json:"type"`
	ID       string            `json:"id"`
	Command  string            `json:"command,omitempty"`
	Path     string            `json:"path,omitempty"`
	Content  string            `json:"content,omitempty"`
	Append   bool              `json:"append,omitempty"`
	Bg       bool              `json:"background,omitempty"`
	Env      map[string]string `json:"env,omitempty"`
	Data     string            `json:"data,omitempty"`
	Encoding string            `json:"encoding,omitempty"`
	Cols     int               `json:"cols,omitempty"`
	Rows     int               `json:"rows,omitempty"`
	// Line-numbered read support
	Offset          int  `json:"offset,omitempty"`            // Lines to skip
	Limit           int  `json:"limit,omitempty"`             // Max lines to return
	ShowLineNumbers bool `json:"show_line_numbers,omitempty"` // Prefix lines with "N\t"
	VsockPort       int  `json:"vsock_port,omitempty"`        // Override vsock port from request
	UseVsock        bool `json:"use_vsock,omitempty"`         // Enable guest->host upload fast path
	UseVsockOutput  bool `json:"use_vsock_output,omitempty"`  // For exec: buffer stdout/stderr and upload via vsock on completion
}

type Event struct {
	Type    string      `json:"type"`
	Payload interface{} `json:"payload"`
}

// =============================================================================
// Sessions
// =============================================================================

type Session struct {
	Process *os.Process
	Stdin   io.WriteCloser
	PTY     *os.File
	CmdID   string
	IsPTY   bool
}

var (
	sessions   = make(map[string]*Session)
	sessionsMu sync.Mutex
)

// =============================================================================
// Console output
// =============================================================================

var (
	consoleMu sync.Mutex
	writer    *bufio.Writer
)

func sendEvent(typ string, payload interface{}) {
	evt := Event{Type: typ, Payload: payload}
	data, err := json.Marshal(evt)
	if err != nil {
		return
	}
	consoleMu.Lock()
	writer.Write(data)
	writer.WriteByte('\n')
	writer.Flush()
	consoleMu.Unlock()
}

// =============================================================================
// Vsock
// =============================================================================

const (
	vsockCIDHost   = 2
	vsockChunkSize = 65536
)

var (
	vsockAvailable int32 // 0=unknown, 1=yes, -1=no
	vsockLastCheck time.Time
	vsockFailCount int
	vsockMu        sync.Mutex
)

func buildSockaddrVM(cid, port uint32) [16]byte {
	var sa [16]byte
	binary.LittleEndian.PutUint16(sa[0:2], 40) // AF_VSOCK
	binary.LittleEndian.PutUint32(sa[4:8], port)
	binary.LittleEndian.PutUint32(sa[8:12], cid)
	return sa
}

func dialVsock(cid, port int, timeout time.Duration) (*os.File, error) {
	fd, err := syscall.Socket(40, syscall.SOCK_STREAM, 0)
	if err != nil {
		return nil, err
	}
	if timeout > 0 {
		tv := syscall.Timeval{
			Sec:  int64(timeout / time.Second),
			Usec: int64(timeout % time.Second / time.Microsecond),
		}
		syscall.SetsockoptTimeval(fd, syscall.SOL_SOCKET, syscall.SO_SNDTIMEO, &tv)
		syscall.SetsockoptTimeval(fd, syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &tv)
	}
	sa := buildSockaddrVM(uint32(cid), uint32(port))
	_, _, e1 := syscall.Syscall(syscall.SYS_CONNECT, uintptr(fd), uintptr(unsafe.Pointer(&sa[0])), 16)
	if e1 != 0 {
		syscall.Close(fd)
		return nil, fmt.Errorf("vsock connect: %v", e1)
	}
	return os.NewFile(uintptr(fd), "vsock"), nil
}

func vsockProbe(port int) bool {
	conn, err := dialVsock(vsockCIDHost, port, 1*time.Second)
	if err != nil {
		vsockMu.Lock()
		atomic.StoreInt32(&vsockAvailable, -1)
		vsockLastCheck = time.Now()
		vsockFailCount++
		vsockMu.Unlock()
		return false
	}
	conn.Close()
	vsockMu.Lock()
	atomic.StoreInt32(&vsockAvailable, 1)
	vsockLastCheck = time.Now()
	vsockFailCount = 0
	vsockMu.Unlock()
	return true
}

func vsockCanUse(port int) bool {
	// No eager probe: the 1s connect timeout in vsockProbe used to be
	// paid on every cold first-read, which is exactly the latency we're
	// trying to remove. Instead, optimistically allow vsock unless we
	// recently saw a hard failure (then back off). vsockCreateConn
	// updates state on success/failure, so the actual transfer connect
	// doubles as the probe.
	state := atomic.LoadInt32(&vsockAvailable)
	if state == 1 {
		return true
	}
	if state == -1 {
		vsockMu.Lock()
		since := time.Since(vsockLastCheck)
		fails := vsockFailCount
		vsockMu.Unlock()
		backoff := time.Duration(int64(1<<uint(fails)) * int64(time.Second))
		if backoff < 2*time.Second {
			backoff = 2 * time.Second
		}
		if backoff > 30*time.Second {
			backoff = 30 * time.Second
		}
		if since < backoff {
			return false
		}
	}
	// Unknown or backoff elapsed — let the real connect be the probe.
	return true
}

func vsockMarkBroken() {
	atomic.StoreInt32(&vsockAvailable, -1)
	vsockMu.Lock()
	vsockLastCheck = time.Now()
	vsockMu.Unlock()
}

func vsockCreateConn(port int, timeout time.Duration) (*os.File, error) {
	conn, err := dialVsock(vsockCIDHost, port, timeout)
	if err == nil {
		// A successful connect is the most reliable signal that vsock
		// is healthy — clear any prior failure state so subsequent
		// reads aren't gated by stale backoff.
		vsockMu.Lock()
		atomic.StoreInt32(&vsockAvailable, 1)
		vsockLastCheck = time.Now()
		vsockFailCount = 0
		vsockMu.Unlock()
		return conn, nil
	}
	vsockMarkBroken()
	return nil, err
}

func vsockSendJSON(conn *os.File, data interface{}) error {
	b, err := json.Marshal(data)
	if err != nil {
		return err
	}
	b = append(b, '\n')
	_, err = conn.Write(b)
	return err
}

func vsockRecvJSON(conn *os.File) (map[string]interface{}, error) {
	return vsockRecvJSONFrom(bufio.NewReader(conn))
}

// vsockRecvJSONFrom reads one JSON line from a reused bufio.Reader.
// The reused reader is critical: when the host sends multiple JSON
// messages back-to-back (e.g. READY+COMPLETE for a zero-byte upload)
// they can land together in the agent's recv buffer. Creating a new
// bufio.Reader per call discards anything not consumed from the
// previous one, so the second message is lost — the agent then reads
// from a closed conn, returns an error, and marks vsock broken,
// degrading every subsequent command on this VM to the UART path.
func vsockRecvJSONFrom(reader *bufio.Reader) (map[string]interface{}, error) {
	line, err := reader.ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	var result map[string]interface{}
	if err := json.Unmarshal(line, &result); err != nil {
		return nil, err
	}
	return result, nil
}

// =============================================================================
// Shell command execution
// =============================================================================

func handleExec(cmdID, command string, background bool, env map[string]string, useVsockOutput bool, vsockPort int) {
	cmd := exec.Command("/bin/sh", "-c", command)
	cmd.Env = os.Environ()
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}

	if background {
		stdout, _ := cmd.StdoutPipe()
		stderr, _ := cmd.StderrPipe()
		stdin, _ := cmd.StdinPipe()

		if err := cmd.Start(); err != nil {
			sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
			sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
			return
		}
		sessionsMu.Lock()
		sessions[cmdID] = &Session{Process: cmd.Process, Stdin: stdin, CmdID: cmdID}
		sessionsMu.Unlock()

		go readStream(stdout, "stdout", cmdID)
		go readStream(stderr, "stderr", cmdID)

		sendEvent("status", map[string]interface{}{"cmd_id": cmdID, "status": "started", "pid": cmd.Process.Pid})

		go func() {
			err := cmd.Wait()
			ec := 0
			if err != nil {
				if ee, ok := err.(*exec.ExitError); ok {
					ec = ee.ExitCode()
				} else {
					ec = 1
				}
			}
			sessionsMu.Lock()
			delete(sessions, cmdID)
			sessionsMu.Unlock()
			sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": ec})
		}()
	} else {
		// Two output paths:
		//   - UART path (default): streamRaw shoves base64-chunked
		//     output events onto the serial console as they arrive.
		//     Uses StdoutPipe/StderrPipe so output streams in real
		//     time (matters for progress indicators).
		//   - Vsock path (useVsockOutput=true): cmd.Stdout / .Stderr
		//     wired straight to capped in-memory buffers. After
		//     cmd.Wait returns we ship each buffer as a single vsock
		//     upload. Doing it this way (instead of StdoutPipe +
		//     goroutine drain) avoids the documented race in
		//     exec.Cmd.Wait closing the pipe before our drain
		//     goroutine has read the buffered bytes — fast commands
		//     like `echo` were losing their entire output to that
		//     race.
		var wg sync.WaitGroup
		var stdoutBuf, stderrBuf *bytes.Buffer
		var stdoutTrunc, stderrTrunc bool
		var stdout, stderr io.ReadCloser
		if useVsockOutput {
			stdoutBuf = &bytes.Buffer{}
			stderrBuf = &bytes.Buffer{}
			cmd.Stdout = &cappedWriter{buf: stdoutBuf, cap: streamRawByteCap, truncated: &stdoutTrunc}
			cmd.Stderr = &cappedWriter{buf: stderrBuf, cap: streamRawByteCap, truncated: &stderrTrunc}
		} else {
			stdout, _ = cmd.StdoutPipe()
			stderr, _ = cmd.StderrPipe()
		}

		if err := cmd.Start(); err != nil {
			sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
			sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
			return
		}

		// Register foreground execs in sessions[] so handleKill can stop
		// them. Without this, a runaway command (e.g. an unbounded
		// `rg -rn` whose host-side caller already raised TimeoutError)
		// keeps producing output and monopolises the serial console
		// `consoleMu`, blocking every other goroutine's sendEvent and
		// wedging the VM. The old code only registered background execs.
		sessionsMu.Lock()
		sessions[cmdID] = &Session{Process: cmd.Process, CmdID: cmdID}
		sessionsMu.Unlock()
		defer func() {
			sessionsMu.Lock()
			delete(sessions, cmdID)
			sessionsMu.Unlock()
		}()

		if !useVsockOutput {
			wg.Add(2)
			go func() { defer wg.Done(); streamRaw(stdout, "stdout", cmdID) }()
			go func() { defer wg.Done(); streamRaw(stderr, "stderr", cmdID) }()
		}

		err := cmd.Wait()
		wg.Wait()

		ec := 0
		if err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				ec = ee.ExitCode()
			} else {
				ec = 1
			}
		}

		if useVsockOutput {
			port := vsockPort
			if port == 0 {
				port = getVsockPort()
			}
			// Try the vsock fast path. Each stream uploads
			// independently — empty streams still upload so the
			// host knows the slot is done (lets the host's
			// buf_slot wait return immediately). On any failure
			// for a non-empty stream, fall back to UART so the
			// caller still gets its output. Empty-stream failures
			// don't trigger fallback because there's nothing to
			// deliver.
			okOut := true
			okErr := true
			if vsockCanUse(port) {
				okOut = uploadBytesViaVsock(cmdID+":stdout", stdoutBuf.Bytes(), port)
				okErr = uploadBytesViaVsock(cmdID+":stderr", stderrBuf.Bytes(), port)
			} else {
				okOut = false
				okErr = false
			}
			if !okOut && stdoutBuf.Len() > 0 {
				flushBufferToSerial(stdoutBuf.Bytes(), "stdout", cmdID)
			}
			if !okErr && stderrBuf.Len() > 0 {
				flushBufferToSerial(stderrBuf.Bytes(), "stderr", cmdID)
			}
			if stdoutTrunc {
				sendEvent("output_truncated", map[string]interface{}{
					"cmd_id": cmdID, "stream": "stdout",
					"bytes_sent": stdoutBuf.Len(),
				})
			}
			if stderrTrunc {
				sendEvent("output_truncated", map[string]interface{}{
					"cmd_id": cmdID, "stream": "stderr",
					"bytes_sent": stderrBuf.Len(),
				})
			}
		}
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": ec})
	}
}

// cappedWriter is an io.Writer that appends to a *bytes.Buffer up to
// `cap` total bytes, then quietly discards the rest while flagging
// `truncated`. Wired directly into exec.Cmd.Stdout / .Stderr so writes
// happen synchronously during the child's runtime — no pipe-drain race
// against cmd.Wait closing the read end.
type cappedWriter struct {
	buf       *bytes.Buffer
	cap       int
	truncated *bool
}

func (w *cappedWriter) Write(p []byte) (int, error) {
	remaining := w.cap - w.buf.Len()
	if remaining <= 0 {
		*w.truncated = true
		return len(p), nil // pretend success so child doesn't SIGPIPE
	}
	if len(p) <= remaining {
		w.buf.Write(p)
		return len(p), nil
	}
	w.buf.Write(p[:remaining])
	*w.truncated = true
	return len(p), nil
}

// flushBufferToSerial emits a buffered exec stream as the same
// `output` events streamRaw would have produced — used as a fallback
// when the vsock upload path fails so the caller still sees output.
func flushBufferToSerial(data []byte, stream, cmdID string) {
	if len(data) == 0 {
		return
	}
	const chunk = 4096
	for i := 0; i < len(data); i += chunk {
		end := i + chunk
		if end > len(data) {
			end = len(data)
		}
		encoded := base64.StdEncoding.EncodeToString(data[i:end])
		sendEvent("output", map[string]interface{}{
			"cmd_id": cmdID, "stream": stream,
			"data": encoded, "encoding": "base64",
		})
	}
}

func readStream(r io.Reader, stream, cmdID string) {
	streamRaw(r, stream, cmdID)
}

// Per-stream output cap. Beyond this we send a single
// "output_truncated" event and quietly drain the rest. This bounds how
// much one runaway command can monopolise the serial UART when sendEvent
// holds consoleMu while bufio.Writer.Flush() blocks on the line-rate
// limited firecracker serial. Without this cap, a single unbounded
// `rg -rn` floods the UART faster than it can drain, every other
// goroutine's sendEvent (including unrelated `exit` events) queues
// behind it, and the entire VM stops emitting events to the host.
const streamRawByteCap = 4 * 1024 * 1024 // 4 MiB per stream per command

// streamRaw forwards raw byte chunks from r as base64-encoded output events.
// Unlike a line-buffered scanner, this delivers data the moment it's available,
// which matters for tools that emit progress with carriage returns and no
// trailing newline (e.g. `git clone --progress`, curl, apt). Base64 keeps the
// JSON event safe from non-UTF8 / control bytes.
func streamRaw(r io.Reader, stream, cmdID string) {
	buf := make([]byte, 4096)
	var sent int64
	truncated := false
	for {
		n, err := r.Read(buf)
		if n > 0 {
			if !truncated {
				remaining := int64(streamRawByteCap) - sent
				if remaining <= 0 {
					sendEvent("output_truncated", map[string]interface{}{
						"cmd_id": cmdID, "stream": stream, "bytes_sent": sent,
					})
					truncated = true
				} else {
					take := int64(n)
					if take > remaining {
						take = remaining
					}
					encoded := base64.StdEncoding.EncodeToString(buf[:take])
					sendEvent("output", map[string]interface{}{
						"cmd_id": cmdID, "stream": stream, "data": encoded, "encoding": "base64",
					})
					sent += take
					if take < int64(n) {
						sendEvent("output_truncated", map[string]interface{}{
							"cmd_id": cmdID, "stream": stream, "bytes_sent": sent,
						})
						truncated = true
					}
				}
			}
			// Once truncated, keep reading (so the child doesn't block on
			// SIGPIPE) but drop the bytes on the floor.
		}
		if err != nil {
			return
		}
	}
}

func handleInput(cmdID, data, encoding string) {
	sessionsMu.Lock()
	sess, ok := sessions[cmdID]
	sessionsMu.Unlock()
	if !ok {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": "Session not found"})
		return
	}
	var content []byte
	if encoding == "base64" {
		decoded, err := base64.StdEncoding.DecodeString(data)
		if err != nil {
			decoded, err = base64.RawStdEncoding.DecodeString(data)
			if err != nil {
				sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("base64 decode failed: %v", err)})
				return
			}
		}
		content = decoded
	} else {
		content = []byte(data)
	}

	writer := sess.Stdin
	if sess.IsPTY && sess.PTY != nil {
		writer = sess.PTY
	}
	if writer == nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": "Session stdin not available"})
		return
	}
	if _, err := writer.Write(content); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("write failed: %v", err)})
	}
}

func handleKill(cmdID string) {
	sessionsMu.Lock()
	sess, ok := sessions[cmdID]
	sessionsMu.Unlock()
	if !ok {
		return
	}
	// SIGTERM first, then SIGKILL after a brief grace, so a runaway
	// command that ignores SIGTERM (or whose shell parent ignores it)
	// can't keep flooding the console indefinitely.
	if sess.Process != nil {
		sess.Process.Signal(syscall.SIGTERM)
		go func(p *os.Process) {
			time.Sleep(500 * time.Millisecond)
			p.Signal(syscall.SIGKILL)
		}(sess.Process)
	}
}

// =============================================================================
// File read — with line numbers, offset, limit, header/footer
// =============================================================================

func handleReadFile(cmdID, path string, offset, limit int, showLineNumbers bool, vsockPort int, useVsock bool) {
	// Only use the vsock upload path when the host explicitly registered a
	// destination for this command (download_file). Plain read_file calls expect
	// file_content/file_chunk events on serial; uploading to the listener would
	// otherwise leave the caller with no content and may write to an unintended
	// host path.
	if useVsock {
		port := vsockPort
		if port == 0 {
			port = getVsockPort()
		}
		if vsockCanUse(port) && handleVsockUpload(cmdID, path, port) {
			return
		}
	}

	handleReadFileSerial(cmdID, path, offset, limit, showLineNumbers)
}

func handleReadFileSerial(cmdID, path string, offset, limit int, showLineNumbers bool) {
	info, err := os.Stat(path)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("File not found: %s", path)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	if info.IsDir() {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("Path is a directory: %s", path)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	if offset > 0 || limit > 0 || showLineNumbers {
		handleReadFileWindow(cmdID, path, offset, limit, showLineNumbers)
		return
	}

	fileSize := info.Size()
	// Bigger chunks → fewer JSON events on the serial console, which
	// matters when many parallel reads are in flight contending for
	// consoleMu. Fewer hand-offs means each read drains faster and
	// sibling reads aren't starved.
	chunkSize := int64(64 * 1024)

	// Small file — send in one shot
	if fileSize <= chunkSize {
		data, err := os.ReadFile(path)
		if err != nil {
			sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
			sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
			return
		}
		encoded := base64.StdEncoding.EncodeToString(data)
		totalLines := bytes.Count(data, []byte{'\n'}) + 1
		sendEvent("file_content", map[string]interface{}{
			"cmd_id": cmdID, "path": path, "content": encoded, "total_lines": totalLines,
		})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
		return
	}

	// Large file — chunked
	f, err := os.Open(path)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	defer f.Close()

	hash := md5.New()
	var off int64
	var lineCount int
	buf := make([]byte, chunkSize)

	for {
		n, err := f.Read(buf)
		if n > 0 {
			hash.Write(buf[:n])
			lineCount += bytes.Count(buf[:n], []byte{'\n'})
			encoded := base64.StdEncoding.EncodeToString(buf[:n])
			sendEvent("file_chunk", map[string]interface{}{
				"cmd_id": cmdID, "path": path, "data": encoded, "offset": off, "size": n,
			})
			off += int64(n)
		}
		if err != nil {
			break
		}
	}

	// Match Python's len(text.split('\n')) semantics.
	lineCount++
	sendEvent("file_complete", map[string]interface{}{
		"cmd_id": cmdID, "path": path, "total_size": fileSize, "checksum": fmt.Sprintf("%x", hash.Sum(nil)), "total_lines": lineCount,
	})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

func handleReadFileWindow(cmdID, path string, offset, limit int, showLineNumbers bool) {
	f, err := os.Open(path)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1024*1024), 16*1024*1024)

	var out []byte
	lineNo := 0
	emitted := 0
	for scanner.Scan() {
		lineNo++
		if lineNo <= offset {
			continue
		}
		if limit > 0 && emitted >= limit {
			continue // keep counting for total_lines
		}
		if showLineNumbers {
			out = strconv.AppendInt(out, int64(lineNo), 10)
			out = append(out, '\t')
		}
		out = append(out, scanner.Bytes()...)
		out = append(out, '\n')
		emitted++
	}
	if err := scanner.Err(); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	if len(out) > 0 && out[len(out)-1] == '\n' {
		out = out[:len(out)-1]
	}

	encoded := base64.StdEncoding.EncodeToString(out)
	// lineNo is the total line count after scanning to EOF.
	sendEvent("file_content", map[string]interface{}{
		"cmd_id": cmdID, "path": path, "content": encoded, "total_lines": lineNo,
	})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

// =============================================================================
// File write — with native append
// =============================================================================

func handleWriteFile(cmdID, path, content string, appendMode bool) {
	dir := filepath.Dir(path)
	if dir != "" {
		os.MkdirAll(dir, 0755)
	}

	decoded, err := base64.StdEncoding.DecodeString(content)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	flag := os.O_WRONLY | os.O_CREATE
	if appendMode {
		flag |= os.O_APPEND
	} else {
		flag |= os.O_TRUNC
	}

	f, err := os.OpenFile(path, flag, 0644)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	defer f.Close()

	if _, err := f.Write(decoded); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	// Skip "status: written" — under 200-concurrent writes the per-event
	// syscall (Flush after each Write under consoleMu) is hot-path
	// pressure. Callers gate on the exit event regardless.
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

func handleWriteText(cmdID, path, content string, appendMode bool) {
	dir := filepath.Dir(path)
	if dir != "" {
		os.MkdirAll(dir, 0755)
	}

	flag := os.O_WRONLY | os.O_CREATE
	if appendMode {
		flag |= os.O_APPEND
	} else {
		flag |= os.O_TRUNC
	}

	f, err := os.OpenFile(path, flag, 0644)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	defer f.Close()

	if _, err := f.WriteString(content); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	// Skip "status: written" — under 200-concurrent writes the per-event
	// syscall (Flush after each Write under consoleMu) is hot-path
	// pressure. Callers gate on the exit event regardless.
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

// =============================================================================
// List directory
// =============================================================================

func handleListDir(cmdID, path string, vsockPort int, useVsock bool) {
	entries, err := os.ReadDir(path)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	files := make([]map[string]interface{}, 0, len(entries))
	for _, entry := range entries {
		info, err := entry.Info()
		if err != nil {
			files = append(files, map[string]interface{}{"name": entry.Name(), "type": "unknown", "size": 0})
			continue
		}
		typ := "file"
		if entry.IsDir() {
			typ = "directory"
		}
		files = append(files, map[string]interface{}{
			"name": entry.Name(), "type": typ, "size": info.Size(),
			"mode": info.Mode(), "mtime": info.ModTime().Unix(),
		})
	}

	// Vsock fast path: upload the JSON listing as bytes to a host-side
	// pending buffer registered for this cmd_id. Skips the serial console
	// entirely so parallel list_dir calls don't queue at consoleMu.
	if useVsock {
		port := vsockPort
		if port == 0 {
			port = getVsockPort()
		}
		payload := map[string]interface{}{"path": path, "files": files}
		if data, err := json.Marshal(payload); err == nil {
			if vsockCanUse(port) && uploadBytesViaVsock(cmdID, data, port) {
				sendEvent("status", map[string]interface{}{"cmd_id": cmdID, "status": "uploaded", "size": len(data)})
				sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
				return
			}
		}
		// Fall through to serial path on any failure.
	}

	sendEvent("dir_list", map[string]interface{}{"cmd_id": cmdID, "path": path, "files": files})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

// uploadBytesViaVsock streams an in-memory blob to the host listener.
// Mirrors handleVsockUpload's protocol but takes []byte instead of a path.
// Returns true on success.
func uploadBytesViaVsock(cmdID string, data []byte, port int) bool {
	conn, err := vsockCreateConn(port, 10*time.Second)
	if err != nil {
		return false
	}
	defer conn.Close()

	// Share one bufio.Reader for all reads on this conn so we don't
	// lose buffered bytes between vsockRecvJSON calls.
	reader := bufio.NewReader(conn)

	if err := vsockSendJSON(conn, map[string]interface{}{
		"type": "upload", "path": "<bytes>", "size": len(data), "checksum": "", "cmd_id": cmdID,
	}); err != nil {
		return false
	}
	resp, err := vsockRecvJSONFrom(reader)
	if err != nil || resp["type"] != "ready" {
		return false
	}
	// Skip the write syscall on empty payloads. The host sends
	// READY+COMPLETE back-to-back for size=0 and closes the connection
	// immediately after — the agent's subsequent write races with that
	// close and returns EPIPE on most kernels, which would trip
	// vsockMarkBroken() and starve the *next* command of vsock.
	if len(data) > 0 {
		if _, err := conn.Write(data); err != nil {
			vsockMarkBroken()
			return false
		}
	}
	resp, err = vsockRecvJSONFrom(reader)
	if err != nil || resp["type"] == "error" {
		vsockMarkBroken()
		return false
	}
	return true
}

// =============================================================================
// File info
// =============================================================================

func handleFileInfo(cmdID, path string) {
	info, err := os.Stat(path)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("Path not found: %s", path)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	sendEvent("file_info", map[string]interface{}{
		"cmd_id": cmdID,
		"info": map[string]interface{}{
			"size":  info.Size(),
			"mode":  fmt.Sprintf("%o", info.Mode()),
			"mtime": info.ModTime().Unix(),
		},
	})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

// =============================================================================
// Vsock upload (read_file fast path: guest → host)
// =============================================================================

func handleVsockUpload(cmdID, path string, port int) bool {
	info, err := os.Stat(path)
	if err != nil {
		return false
	}
	fileSize := info.Size()

	conn, err := vsockCreateConn(port, 10*time.Second)
	if err != nil {
		return false
	}
	defer conn.Close()

	if err := vsockSendJSON(conn, map[string]interface{}{
		// Empty checksum asks the host to trust the reliable vsock stream and
		// avoids an extra full-file read in the guest before upload.
		"type": "upload", "path": path, "size": fileSize, "checksum": "", "cmd_id": cmdID,
	}); err != nil {
		return false
	}

	resp, err := vsockRecvJSON(conn)
	if err != nil || resp["type"] != "ready" {
		return false
	}

	// Stream file
	f, err := os.Open(path)
	if err != nil {
		return false
	}
	defer f.Close()

	buf := make([]byte, vsockChunkSize)
	for {
		n, err := f.Read(buf)
		if n > 0 {
			if _, werr := conn.Write(buf[:n]); werr != nil {
				vsockMarkBroken()
				return false
			}
		}
		if err != nil {
			break
		}
	}

	resp, err = vsockRecvJSON(conn)
	if err != nil || resp["type"] == "error" {
		vsockMarkBroken()
		return false
	}

	sendEvent("status", map[string]interface{}{
		"cmd_id": cmdID, "status": "uploaded", "size": fileSize,
	})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
	return true
}

// =============================================================================
// Vsock download (write_file fast path: host → guest)
// =============================================================================

func handleVsockDownload(cmdID, path string, port int, appendMode bool) {
	conn, err := vsockCreateConn(port, 10*time.Second)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("Vsock connect failed: %v", err)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	defer conn.Close()

	if err := vsockSendJSON(conn, map[string]interface{}{
		"type": "download_raw", "path": path, "cmd_id": cmdID,
	}); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	dir := filepath.Dir(path)
	if dir != "" {
		os.MkdirAll(dir, 0755)
	}

	flag := os.O_WRONLY | os.O_CREATE
	if appendMode {
		flag |= os.O_APPEND
	} else {
		flag |= os.O_TRUNC
	}
	f, err := os.OpenFile(path, flag, 0644)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	defer f.Close()

	reader := bufio.NewReaderSize(conn, vsockChunkSize)
	line, err := reader.ReadBytes('\n')
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("Connection closed: %v", err)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	var resp map[string]interface{}
	if err := json.Unmarshal(line, &resp); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	if resp["type"] == "error" {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": resp["error"]})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	if resp["type"] != "ready" {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": "unexpected download response"})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	sizeFloat, _ := resp["size"].(float64)
	size := int64(sizeFloat)
	checksum, _ := resp["checksum"].(string)
	hash := md5.New()
	written, err := io.CopyN(io.MultiWriter(f, hash), reader, size)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	if written != size {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("short download: expected %d, got %d", size, written)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}
	if checksum != "" {
		got := fmt.Sprintf("%x", hash.Sum(nil))
		if got != checksum {
			sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("Checksum mismatch: expected %s, got %s", checksum, got)})
			sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
			return
		}
	}

	// Skip "status: written" — under 200-concurrent writes the per-event
	// syscall (Flush after each Write under consoleMu) is hot-path
	// pressure. Callers gate on the exit event regardless.
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

// =============================================================================
// PTY exec
// =============================================================================

func handlePTYExec(cmdID, command string, cols, rows int, env map[string]string) {
	shell := os.Getenv("SHELL")
	if shell == "" {
		shell = "/bin/sh"
	}

	cmd := exec.Command(shell, "-c", command)
	cmd.Env = os.Environ()
	for k, v := range env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	cmd.Env = append(cmd.Env, fmt.Sprintf("COLUMNS=%d", cols), fmt.Sprintf("LINES=%d", rows))

	ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	sessionsMu.Lock()
	sessions[cmdID] = &Session{Process: cmd.Process, Stdin: ptmx, PTY: ptmx, CmdID: cmdID, IsPTY: true}
	sessionsMu.Unlock()

	sendEvent("status", map[string]interface{}{"cmd_id": cmdID, "status": "started"})

	// Read PTY output with base64 encoding for binary safety
	go func() {
		defer ptmx.Close()
		buf := make([]byte, 1024)
		for {
			n, err := ptmx.Read(buf)
			if n > 0 {
				encoded := base64.StdEncoding.EncodeToString(buf[:n])
				sendEvent("output", map[string]interface{}{
					"cmd_id": cmdID, "stream": "stdout", "data": encoded, "encoding": "base64",
				})
			}
			if err != nil {
				break
			}
		}
	}()

	go func() {
		err := cmd.Wait()
		ec := 0
		if err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				ec = ee.ExitCode()
			} else {
				ec = 1
			}
		}
		sessionsMu.Lock()
		delete(sessions, cmdID)
		sessionsMu.Unlock()
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": ec})
	}()
}

func handleResize(cmdID string, cols, rows int) {
	sessionsMu.Lock()
	sess, ok := sessions[cmdID]
	sessionsMu.Unlock()
	if !ok || !sess.IsPTY || sess.PTY == nil {
		return
	}
	_ = pty.Setsize(sess.PTY, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
}

// =============================================================================
// Main loop
// =============================================================================

// getVsockPort reads BANDSOX_VSOCK_PORT lazily — the host sets this env var
// after the agent starts (in configure()), so we must re-read it each time.
func getVsockPort() int {
	if s := os.Getenv("BANDSOX_VSOCK_PORT"); s != "" {
		if p, err := strconv.Atoi(s); err == nil {
			return p
		}
	}
	return 9000
}

func main() {
	writer = bufio.NewWriterSize(os.Stdout, 65536)

	sendEvent("status", map[string]interface{}{"status": "ready"})

	// Stdin reader: 1MB initial buffer, 64MB cap. The 1MB cap from the
	// previous version silently dropped any JSON request larger than 1MB
	// (e.g. write_file with base64 content > ~750KB binary), producing
	// no response and a 30s timeout on the host.
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 1024*1024), 64*1024*1024)

	for scanner.Scan() {
		var req Request
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			continue
		}

		switch req.Type {
		case "exec":
			go handleExec(req.ID, req.Command, req.Bg, req.Env, req.UseVsockOutput, req.VsockPort)

		case "pty_exec":
			handlePTYExec(req.ID, req.Command, req.Cols, req.Rows, req.Env)

		case "input":
			handleInput(req.ID, req.Data, req.Encoding)

		case "resize":
			handleResize(req.ID, req.Cols, req.Rows)

		case "kill":
			handleKill(req.ID)

		case "read_file":
			go handleReadFile(req.ID, req.Path, req.Offset, req.Limit, req.ShowLineNumbers, req.VsockPort, req.UseVsock)

		case "write_file":
			go handleWriteFile(req.ID, req.Path, req.Content, req.Append)

		case "write_text":
			go handleWriteText(req.ID, req.Path, req.Content, req.Append)

		case "write_file_vsock":
			// Fast path: download file from host via vsock
			go handleVsockDownload(req.ID, req.Path, req.VsockPort, req.Append)

		case "list_dir":
			go handleListDir(req.ID, req.Path, req.VsockPort, req.UseVsock)

		case "file_info":
			go handleFileInfo(req.ID, req.Path)

		default:
			// Backward compat: treat unknown types with a command field as exec
			if req.Command != "" {
				go handleExec(req.ID, req.Command, false, req.Env, req.UseVsockOutput, req.VsockPort)
			}
		}
	}
}
