// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Envelope authentication byte-compatible with scripts/lan_bridge/
// lan_bridge.py seal/open_envelope: HMAC-SHA256 over the exact payload
// bytes, kind/id binding, constant-time MAC compare.
package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"strings"
)

const maxEnvelopeBytes = 2 * 1024 * 1024

func isHex32(s string) bool {
	if len(s) != 32 {
		return false
	}
	for i := 0; i < 32; i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func isHex64(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < 64; i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func sha256Hex(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// seal renders the payload in canonical form and wraps it in a v1 envelope.
// Field order and MAC placement match the canonical bridge; any compliant
// reader opens it because authentication is over the payload bytes.
func seal(payload map[string]any, key []byte, kind, id string) ([]byte, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("invalid key")
	}
	payloadText, err := canonicalJSON(payload)
	if err != nil {
		return nil, err
	}
	payloadBytes := []byte(payloadText)
	mac := hmac.New(sha256.New, key)
	mac.Write(payloadBytes)
	macHex := hex.EncodeToString(mac.Sum(nil))
	env := `{"v":1,"kind":` + quoteJSONString(kind) +
		`,"id":` + quoteJSONString(id) +
		`,"payload_b64":` + quoteJSONString(base64.StdEncoding.EncodeToString(payloadBytes)) +
		`,"mac":` + quoteJSONString(macHex) + `}`
	envelopeBytes := []byte(env)
	if len(envelopeBytes) > maxEnvelopeBytes {
		return nil, fmt.Errorf("envelope too large")
	}
	return envelopeBytes, nil
}

func openEnvelope(data, key []byte, expectedKind, expectedID string) (map[string]any, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("invalid key")
	}
	if len(data) > maxEnvelopeBytes {
		return nil, fmt.Errorf("invalid envelope")
	}
	parsed, err := parseJSONBytes(data)
	if err != nil {
		return nil, fmt.Errorf("malformed envelope (%v)", shortErr(err))
	}
	env, ok := asMap(parsed)
	if !ok {
		return nil, fmt.Errorf("malformed envelope")
	}
	if v, ok := asInt(env["v"]); !ok || v != 1 {
		return nil, fmt.Errorf("bad version")
	}
	if k, ok := asString(env["kind"]); !ok || k != expectedKind {
		return nil, fmt.Errorf("bad kind")
	}
	pid, ok := asString(env["id"])
	if !ok || pid != expectedID || !isHex32(expectedID) {
		return nil, fmt.Errorf("bad id")
	}
	b64, ok := asString(env["payload_b64"])
	if !ok {
		return nil, fmt.Errorf("malformed envelope")
	}
	macText, ok := asString(env["mac"])
	if !ok {
		return nil, fmt.Errorf("malformed envelope")
	}
	macLower := strings.ToLower(macText)
	if len(macLower) != 64 || !isHex64(macLower) {
		return nil, fmt.Errorf("bad mac")
	}
	payloadBytes, err := strictBase64Decode(b64)
	if err != nil {
		return nil, fmt.Errorf("malformed base64")
	}
	mac := hmac.New(sha256.New, key)
	mac.Write(payloadBytes)
	calc := mac.Sum(nil)
	want, err := hex.DecodeString(macLower)
	if err != nil || subtle.ConstantTimeCompare(calc, want) != 1 {
		return nil, fmt.Errorf("bad mac")
	}
	payloadParsed, err := parseJSONBytes(payloadBytes)
	if err != nil {
		return nil, fmt.Errorf("malformed payload")
	}
	payload, ok := asMap(payloadParsed)
	if !ok {
		return nil, fmt.Errorf("malformed payload")
	}
	if k, ok := asString(payload["kind"]); !ok || k != expectedKind {
		return nil, fmt.Errorf("bad payload")
	}
	if p, ok := asString(payload["id"]); !ok || p != expectedID {
		return nil, fmt.Errorf("bad payload")
	}
	return payload, nil
}
