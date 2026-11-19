// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Key custody: the key stays on a local fixed disk and outside the queue,
// the shared folder, and the canonical checkout. UNC, device aliases, and
// mapped network drives refuse fail-closed.
package main

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

func stripExtendedPrefix(full string) string {
	upper := strings.ToUpper(full)
	switch {
	case strings.HasPrefix(upper, `\\?\UNC\`):
		return `\\` + full[len(`\\?\UNC\`):]
	case strings.HasPrefix(upper, `\\.\UNC\`):
		return `\\` + full[len(`\\.\UNC\`):]
	case strings.HasPrefix(upper, `\\?\`):
		return full[len(`\\?\`):]
	case strings.HasPrefix(upper, `\\.\`):
		return full[len(`\\.\`):]
	}
	return full
}

func normalizedForCompare(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return strings.ToUpper(path)
	}
	full := stripExtendedPrefix(abs)
	full = strings.TrimRight(full, `\/`)
	return strings.ToUpper(full)
}

func withinOrEqual(child, parent string) bool {
	c := normalizedForCompare(child)
	p := normalizedForCompare(parent)
	if c == p {
		return true
	}
	return strings.HasPrefix(c, p+`\`)
}

var (
	modKernel32      = syscall.NewLazyDLL("kernel32.dll")
	procGetDriveType = modKernel32.NewProc("GetDriveTypeW")
)

const (
	driveUnknown   = 0
	driveNoRootDir = 1
	driveRemote    = 4
)

// isKeyPathRemoteOrUnprovable reports whether the key path cannot be proven
// local. Mirrors reasons.cwd_safety (unc / network-drive / unverifiable).
func isKeyPathRemoteOrUnprovable(keyFile string) (bool, string) {
	raw := strings.ReplaceAll(keyFile, "/", `\`)
	upper := strings.ToUpper(raw)
	if strings.HasPrefix(upper, `\\?\UNC\`) || strings.HasPrefix(upper, `\\.\UNC\`) {
		return true, "key path is a UNC path"
	}
	if strings.HasPrefix(upper, `\\?\`) || strings.HasPrefix(upper, `\\.\`) {
		return true, "key path uses a device prefix and cannot be proven local"
	}
	if strings.HasPrefix(raw, `\\`) {
		return true, "key path is a UNC path"
	}
	full := stripExtendedPrefix(mustAbs(keyFile))
	root := volumeRoot(full)
	if root == "" {
		return true, "key path has no provable root"
	}
	kind, err := driveType(root)
	if err != nil {
		return true, "key drive type unobservable (" + shortErr(err) + ")"
	}
	switch kind {
	case driveRemote:
		return true, "key path is on a mapped network drive"
	case driveUnknown, driveNoRootDir:
		return true, "key drive cannot be proven local"
	}
	return false, ""
}

func mustAbs(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return abs
}

func volumeRoot(full string) string {
	vol := filepath.VolumeName(full)
	if vol == "" {
		return ""
	}
	if !strings.HasSuffix(vol, `\`) && !strings.HasSuffix(vol, `:`) {
		return vol
	}
	if strings.HasSuffix(vol, ":") {
		return vol + `\`
	}
	return vol
}

func driveType(root string) (uint32, error) {
	p, err := syscall.UTF16PtrFromString(root)
	if err != nil {
		return 0, err
	}
	r, _, _ := procGetDriveType.Call(uintptr(unsafe.Pointer(p)))
	return uint32(r), nil
}

func isFilesystemRoot(path string) bool {
	full := normalizedForCompare(path)
	abs := mustAbs(stripExtendedPrefix(path))
	root := volumeRoot(stripExtendedPrefix(abs))
	if root == "" {
		return true
	}
	normRoot := strings.TrimRight(strings.ToUpper(stripExtendedPrefix(root)), `\/`)
	return full == normRoot
}

// checkoutRootFromWorkerPy derives the repo root from the canonical
// worker.py path (<repo>/scripts/lenovo_worker/worker.py). "" means the
// checkout cannot be proven; queue + shared custody still applies and the
// canonical worker re-enforces its own custody at --invoke via --shared.
func checkoutRootFromWorkerPy(workerPy string) string {
	if workerPy == "" {
		return ""
	}
	abs, err := filepath.Abs(workerPy)
	if err != nil {
		return ""
	}
	root := filepath.Join(filepath.Dir(abs), "..", "..")
	if isFilesystemRoot(root) {
		return ""
	}
	return root
}

func loadKeyBytes(keyFile, keyFormat string) ([]byte, error) {
	raw, err := os.ReadFile(keyFile)
	if err != nil {
		return nil, refuse("key-unusable",
			"restore the key file named by the Lenovo config", "cannot read key file")
	}
	switch keyFormat {
	case "raw":
		if len(raw) != 32 {
			return nil, refuse("key-unusable",
				"re-issue the 32-byte key outside queue/share/checkout",
				"raw key must be exactly 32 bytes")
		}
		out := make([]byte, 32)
		copy(out, raw)
		return out, nil
	case "base64":
		// Canonical key file: ASCII text, outer whitespace stripped, then
		// strict alphabet/padding (validate=True). Internal whitespace or
		// non-ASCII refuses; it never decodes to a key.
		rawText := string(raw)
		for i := 0; i < len(rawText); i++ {
			if rawText[i] > 127 {
				return nil, refuse("key-unusable",
					"re-issue the 32-byte key outside queue/share/checkout",
					"base64 key must decode to 32 bytes")
			}
		}
		text := strings.TrimSpace(rawText)
		key, err := strictBase64Decode(text)
		if err != nil || len(key) != 32 {
			return nil, refuse("key-unusable",
				"re-issue the 32-byte key outside queue/share/checkout",
				"base64 key must decode to 32 bytes")
		}
		return key, nil
	}
	return nil, refuse("key-unusable",
		"re-issue the 32-byte key outside queue/share/checkout",
		"key unusable (unknown format)")
}
