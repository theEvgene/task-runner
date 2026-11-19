// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Sanitization and strict decoding shared by all gates.
// Receipts and stderr must never carry absolute paths or secrets:
// callers pass every external error through sanitizeText via shortErr
// and every refusal detail through emitRefusal (which sanitizes again).
package main

import (
	"encoding/base64"
	"strings"
)

const redactedPath = "[redacted-path]"

// sanitizeText redacts absolute path tokens. A token starts at a UNC
// prefix (\\), a drive-absolute prefix (X:\ or X:/), or a Unix-absolute
// slash in a delimiter-led position, and runs to the next delimiter.
// Hex digests and reason slugs contain no such prefix and pass through.
func sanitizeText(s string) string {
	if s == "" {
		return s
	}
	var b strings.Builder
	b.Grow(len(s))
	i := 0
	for i < len(s) {
		if isPathStart(s, i) {
			j := i
			for j < len(s) && !isDelim(s[j]) {
				j++
			}
			b.WriteString(redactedPath)
			i = j
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

func isDelim(c byte) bool {
	switch c {
	case ' ', '\t', '\r', '\n', '"', '\'', '(', ')', '[', ']', '{', '}', ',', ';':
		return true
	}
	return false
}

// isPathStart reports whether an absolute path token starts at s[i].
func isPathStart(s string, i int) bool {
	n := len(s)
	// UNC or device prefix: \\ at token start.
	if i+1 < n && s[i] == '\\' && s[i+1] == '\\' {
		if i == 0 || isDelim(s[i-1]) || s[i-1] == '=' || s[i-1] == ':' {
			return true
		}
		return false
	}
	// Drive-absolute: X:\ or X:/ .
	if i+2 < n && isDriveLetter(s[i]) && s[i+1] == ':' && (s[i+2] == '\\' || s[i+2] == '/') {
		if i == 0 || isDelim(s[i-1]) || s[i-1] == '=' || s[i-1] == '(' || s[i-1] == '\'' || s[i-1] == '"' {
			return true
		}
		// Even mid-token (open C:\...) the prefix is unambiguous; redact.
		return true
	}
	// Unix-absolute: / led by a delimiter, start, = or : .
	if s[i] == '/' && i+1 < n && s[i+1] != ' ' && s[i+1] != '"' && s[i+1] != '\'' {
		if i == 0 || isDelim(s[i-1]) || s[i-1] == '=' || s[i-1] == ':' || s[i-1] == '(' {
			// Avoid redacting the JSON separators themselves: require
			// at least one more path char after the slash.
			return true
		}
	}
	return false
}

func isDriveLetter(c byte) bool {
	return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
}

// strictBase64Decode matches the canonical bridge (b64decode validate=True):
// no whitespace anywhere, ASCII alphabet only, correct padding. Go's
// StdEncoding silently ignores \r\n, so it must not be used directly.
func strictBase64Decode(s string) ([]byte, error) {
	if s == "" {
		return nil, errBase64("empty base64")
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\v' {
			return nil, errBase64("whitespace in base64")
		}
		if c > 127 {
			return nil, errBase64("non-ascii base64")
		}
		ok := (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
			(c >= '0' && c <= '9') || c == '+' || c == '/' || c == '='
		if !ok {
			return nil, errBase64("bad base64 alphabet")
		}
	}
	if len(s)%4 != 0 {
		return nil, errBase64("bad base64 length")
	}
	// Padding, if any, is at most two trailing '='.
	pad := 0
	for pad < 2 && pad < len(s) && s[len(s)-1-pad] == '=' {
		pad++
	}
	for i := 0; i < len(s)-pad; i++ {
		if s[i] == '=' {
			return nil, errBase64("misplaced base64 padding")
		}
	}
	return base64.StdEncoding.DecodeString(s)
}

type base64Err string

func errBase64(m string) error { return base64Err(m) }

func (e base64Err) Error() string { return string(e) }
