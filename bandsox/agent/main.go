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
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"
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
	return vsockProbe(port)
}

func vsockMarkBroken() {
	atomic.StoreInt32(&vsockAvailable, -1)
	vsockMu.Lock()
	vsockLastCheck = time.Now()
	vsockMu.Unlock()
}

func vsockCreateConn(port int, timeout time.Duration) (*os.File, error) {
	conn, err := dialVsock(vsockCIDHost, port, timeout)
	if err != nil {
		vsockMarkBroken()
		return nil, err
	}
	return conn, nil
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
	reader := bufio.NewReader(conn)
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

func handleExec(cmdID, command string, background bool, env map[string]string) {
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
		var outBuf, errBuf bytes.Buffer
		cmd.Stdout = &outBuf
		cmd.Stderr = &errBuf

		err := cmd.Run()
		ec := 0
		if err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				ec = ee.ExitCode()
			} else {
				ec = 1
			}
		}

		// Stream captured output line by line
		for _, line := range strings.SplitAfter(outBuf.String(), "\n") {
			if line != "" {
				sendEvent("output", map[string]interface{}{
					"cmd_id": cmdID, "stream": "stdout", "data": line,
				})
			}
		}
		for _, line := range strings.SplitAfter(errBuf.String(), "\n") {
			if line != "" {
				sendEvent("output", map[string]interface{}{
					"cmd_id": cmdID, "stream": "stderr", "data": line,
				})
			}
		}
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": ec})
	}
}

func readStream(r io.Reader, stream, cmdID string) {
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 65536), 65536)
	for scanner.Scan() {
		sendEvent("output", map[string]interface{}{
			"cmd_id": cmdID, "stream": stream, "data": scanner.Text() + "\n",
		})
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
		content, _ = base64.StdEncoding.DecodeString(data)
	} else {
		content = []byte(data)
	}
	if sess.Stdin != nil {
		sess.Stdin.Write(content)
	}
}

func handleKill(cmdID string) {
	sessionsMu.Lock()
	sess, ok := sessions[cmdID]
	sessionsMu.Unlock()
	if !ok {
		return
	}
	sess.Process.Signal(syscall.SIGTERM)
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

	handleReadFileSerial(cmdID, path)
}

func handleReadFileSerial(cmdID, path string) {
	// Serial fallback: send raw content, host applies formatting.
	info, err := os.Stat(path)
	if err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": fmt.Sprintf("File not found: %s", path)})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	fileSize := info.Size()
	chunkSize := int64(8 * 1024)

	// Small file — send in one shot
	if fileSize <= chunkSize {
		data, err := os.ReadFile(path)
		if err != nil {
			sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
			sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
			return
		}
		encoded := base64.StdEncoding.EncodeToString(data)
		sendEvent("file_content", map[string]interface{}{
			"cmd_id": cmdID, "path": path, "content": encoded,
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
	buf := make([]byte, chunkSize)

	for {
		n, err := f.Read(buf)
		if n > 0 {
			hash.Write(buf[:n])
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

	sendEvent("file_complete", map[string]interface{}{
		"cmd_id": cmdID, "path": path, "total_size": fileSize, "checksum": fmt.Sprintf("%x", hash.Sum(nil)),
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

	sendEvent("status", map[string]interface{}{"cmd_id": cmdID, "status": "written"})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
}

// =============================================================================
// List directory
// =============================================================================

func handleListDir(cmdID, path string) {
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

	sendEvent("dir_list", map[string]interface{}{"cmd_id": cmdID, "path": path, "files": files})
	sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 0})
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

func handleVsockDownload(cmdID, path string, port int) {
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

	f, err := os.Create(path)
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

	sendEvent("status", map[string]interface{}{"cmd_id": cmdID, "status": "written"})
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

	stdout, _ := cmd.StdoutPipe()
	stderr, _ := cmd.StderrPipe()
	stdin, _ := cmd.StdinPipe()

	if err := cmd.Start(); err != nil {
		sendEvent("error", map[string]interface{}{"cmd_id": cmdID, "error": err.Error()})
		sendEvent("exit", map[string]interface{}{"cmd_id": cmdID, "exit_code": 1})
		return
	}

	sessionsMu.Lock()
	sessions[cmdID] = &Session{Process: cmd.Process, Stdin: stdin, CmdID: cmdID, IsPTY: true}
	sessionsMu.Unlock()

	sendEvent("status", map[string]interface{}{"cmd_id": cmdID, "status": "started"})

	// Read stdout with base64 encoding for binary safety
	go func() {
		buf := make([]byte, 1024)
		for {
			n, err := stdout.Read(buf)
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

	go readStream(stderr, "stderr", cmdID)

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
	// For pipe-based PTY we signal size via environment in the child,
	// but can't do live resize without a real PTY. No-op.
	_ = cmdID
	_ = cols
	_ = rows
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

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 1024*1024), 1024*1024)

	for scanner.Scan() {
		var req Request
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			continue
		}

		switch req.Type {
		case "exec":
			go handleExec(req.ID, req.Command, req.Bg, req.Env)

		case "pty_exec":
			go handlePTYExec(req.ID, req.Command, req.Cols, req.Rows, req.Env)

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

		case "write_file_vsock":
			// Fast path: download file from host via vsock
			go handleVsockDownload(req.ID, req.Path, req.VsockPort)

		case "list_dir":
			go handleListDir(req.ID, req.Path)

		case "file_info":
			go handleFileInfo(req.ID, req.Path)

		default:
			// Backward compat: treat unknown types with a command field as exec
			if req.Command != "" {
				go handleExec(req.ID, req.Command, false, req.Env)
			}
		}
	}
}
