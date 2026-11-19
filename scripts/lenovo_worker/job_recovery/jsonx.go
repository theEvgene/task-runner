// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Canonical JSON: parsed with json.Number, serialized with sorted keys,
// separators "," and ":", raw UTF-8. Matches Python
// json.dumps(ensure_ascii=False, separators=(",",":")) for the ASCII
// fixture payloads this tool seals and opens.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

func parseJSON(text string) (any, error) {
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	if dec.More() {
		return nil, fmt.Errorf("trailing characters")
	}
	return v, nil
}

func parseJSONBytes(data []byte) (any, error) { return parseJSON(string(data)) }

// canonicalJSON renders maps with ordinal-sorted keys. Slices keep order.
// Only used for digests and equality, never for authentication (the MAC
// covers the transmitted envelope bytes, opened by key, not by re-parse).
func canonicalJSON(v any) (string, error) {
	var sb strings.Builder
	if err := writeCanonical(&sb, v); err != nil {
		return "", err
	}
	return sb.String(), nil
}

func writeCanonical(sb *strings.Builder, v any) error {
	switch t := v.(type) {
	case nil:
		sb.WriteString("null")
	case string:
		sb.WriteString(quoteJSONString(t))
	case bool:
		if t {
			sb.WriteString("true")
		} else {
			sb.WriteString("false")
		}
	case json.Number:
		s := string(t)
		if _, err := strconv.ParseInt(s, 10, 64); err == nil {
			sb.WriteString(s)
			return nil
		}
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return fmt.Errorf("bad number")
		}
		sb.WriteString(strconv.FormatFloat(f, 'g', -1, 64))
	case float64:
		sb.WriteString(strconv.FormatFloat(t, 'g', -1, 64))
	case int:
		sb.WriteString(strconv.Itoa(t))
	case int64:
		sb.WriteString(strconv.FormatInt(t, 10))
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		sb.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				sb.WriteByte(',')
			}
			sb.WriteString(quoteJSONString(k))
			sb.WriteByte(':')
			if err := writeCanonical(sb, t[k]); err != nil {
				return err
			}
		}
		sb.WriteByte('}')
	case []any:
		sb.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				sb.WriteByte(',')
			}
			if err := writeCanonical(sb, e); err != nil {
				return err
			}
		}
		sb.WriteByte(']')
	default:
		return fmt.Errorf("unsupported JSON value type")
	}
	return nil
}

// quoteJSONString encodes one string with HTML escaping disabled, matching
// Python ensure_ascii=False for ASCII payloads (no <>& escaping).
func quoteJSONString(s string) string {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(s); err != nil {
		return strconv.Quote(s)
	}
	return strings.TrimSuffix(buf.String(), "\n")
}

func asMap(v any) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	return m, ok
}

func asList(v any) ([]any, bool) {
	l, ok := v.([]any)
	return l, ok
}

func asString(v any) (string, bool) {
	s, ok := v.(string)
	return s, ok
}

// asInt accepts only true JSON integers (never bool/float), mirroring the
// canonical Python isinstance(pid, int) and not isinstance(pid, bool).
func asInt(v any) (int64, bool) {
	switch t := v.(type) {
	case json.Number:
		n, err := t.Int64()
		if err != nil {
			return 0, false
		}
		return n, true
	case int:
		return int64(t), true
	case int64:
		return t, true
	}
	return 0, false
}
