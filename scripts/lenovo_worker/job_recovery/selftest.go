// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Fixture-only self tests: no live queue, no real key, no config.
package main

import (
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

func selfTest() int {
	passed := 0
	failed := 0
	var log strings.Builder
	runCase(&log, "envelope-roundtrip", func() bool {
		key := testKey(0)
		payload := map[string]any{
			"kind": "reconciliation", "id": "ab010101010101010101010101010101",
			"schema": proofSchema,
		}
		sealed, err := seal(payload, key, "reconciliation", "ab010101010101010101010101010101")
		if err != nil {
			return false
		}
		opened, err := openEnvelope(sealed, key, "reconciliation", "ab010101010101010101010101010101")
		if err != nil {
			return false
		}
		s, _ := asString(opened["schema"])
		return s == proofSchema
	}, &passed, &failed)

	runCase(&log, "envelope-tamper-refused", func() bool {
		key := testKeyReversed()
		payload := map[string]any{
			"kind": "claim", "id": "cd0123456789abcdef0123456789abcdef",
			"schema": claimSchema,
		}
		sealed, err := seal(payload, key, "claim", "cd0123456789abcdef0123456789abcdef")
		if err != nil {
			return false
		}
		sealed[len(sealed)/2] ^= 0x01
		_, err = openEnvelope(sealed, key, "claim", "cd0123456789abcdef0123456789abcdef")
		return err != nil
	}, &passed, &failed)

	runCase(&log, "wrong-key-refused", func() bool {
		k1 := testKey(0)
		k2 := testKey(1)
		payload := map[string]any{
			"kind": "job", "id": "ef0123456789abcdef0123456789abcdef",
		}
		sealed, err := seal(payload, k1, "job", "ef0123456789abcdef0123456789abcdef")
		if err != nil {
			return false
		}
		_, err = openEnvelope(sealed, k2, "job", "ef0123456789abcdef0123456789abcdef")
		return err != nil
	}, &passed, &failed)

	runCase(&log, "owner-alive-refuses", func() bool {
		alive, err := isAlive(int64(os.Getpid()))
		return err == nil && alive
	}, &passed, &failed)

	runCase(&log, "dead-pid-absent", func() bool {
		// 4194303 exceeds the Windows pid range: it cannot exist.
		alive, err := isAlive(4194303)
		return err == nil && !alive
	}, &passed, &failed)

	runCase(&log, "live-child-tree-refuses", func() bool {
		cmd := exec.Command("cmd.exe", "/c", "ping -n 30 127.0.0.1 >nul")
		if err := cmd.Start(); err != nil {
			return false
		}
		pid := int64(cmd.Process.Pid)
		defer func() {
			_ = cmd.Process.Kill()
			_, _ = cmd.Process.Wait()
		}()
		time.Sleep(500 * time.Millisecond)
		alive, _, err := claimedTreeAlive(pid)
		return err == nil && alive
	}, &passed, &failed)

	runCase(&log, "dead-tree-absent", func() bool {
		cmd := exec.Command("cmd.exe", "/c", "exit 0")
		if err := cmd.Start(); err != nil {
			return false
		}
		pid := int64(cmd.Process.Pid)
		_, _ = cmd.Process.Wait()
		for i := 0; i < 100; i++ {
			alive, err := isAlive(pid)
			if err != nil || !alive {
				break
			}
			time.Sleep(50 * time.Millisecond)
		}
		alive, _, err := claimedTreeAlive(pid)
		return err == nil && !alive
	}, &passed, &failed)

	runCase(&log, "binding-mismatch-refused", func() bool {
		claim := map[string]any{
			"schema": claimSchema, "kind": "claim",
			"id": "ab010101010101010101010101010101", "state": heldState,
			"generation":          "ab010101010101010101010101010101",
			"owner":               map[string]any{"instance": "fixture", "pid": jsonNum(4194303)},
			"job_envelope_sha256": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
			"resources":           []any{"exclusive:global"},
		}
		err := validateClaim(claim, "ab010101010101010101010101010101",
			"0000000000000000000000000000000000000000000000000000000000000000")
		if r, ok := err.(*Refusal); ok {
			return r.Reason == "claim-binding-mismatch"
		}
		return false
	}, &passed, &failed)

	runCase(&log, "start-owner-mismatch-refused", func() bool {
		claim := map[string]any{
			"generation":          "ab010101010101010101010101010101",
			"owner":               map[string]any{"instance": "a", "pid": jsonNum(1111)},
			"resources":           []any{"exclusive:global"},
			"job_envelope_sha256": "e2612f2a5d7f0e8f8b1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1",
		}
		start := map[string]any{
			"schema": startSchema, "kind": "start",
			"id":                  "ab010101010101010101010101010101",
			"generation":          "ab010101010101010101010101010101",
			"owner":               map[string]any{"instance": "b", "pid": jsonNum(2222)},
			"child_pid":           jsonNum(3333),
			"job_envelope_sha256": "e2612f2a5d7f0e8f8b1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1",
			"resources":           []any{"exclusive:global"},
		}
		err := validateStart(start, claim, "ab010101010101010101010101010101",
			"e2612f2a5d7f0e8f8b1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1e9a0e1")
		if r, ok := err.(*Refusal); ok {
			return r.Reason == "start-mismatch"
		}
		return false
	}, &passed, &failed)

	runCase(&log, "proof-seal-idempotent", func() bool {
		key := testKeyMul(7)
		proof := map[string]any{
			"schema": proofSchema, "kind": "reconciliation",
			"id":                  "ab010101010101010101010101010101",
			"claim_generation":    "ab010101010101010101010101010101",
			"job_envelope_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
			"resources":           []any{"exclusive:global"},
			"evidence": map[string]any{
				"schema":      evidenceSchema,
				"owner":       map[string]any{"instance": "fixture", "pid": jsonNum(4194303)},
				"child_pid":   jsonNum(4194302),
				"tree_absent": true,
				"observer":    "self-test",
				"observed_at": "2026-11-18T00:00:00+00:00",
			},
		}
		a, err := seal(proof, key, "reconciliation", "ab010101010101010101010101010101")
		if err != nil {
			return false
		}
		b, err := seal(proof, key, "reconciliation", "ab010101010101010101010101010101")
		if err != nil || len(a) != len(b) {
			return false
		}
		for i := range a {
			if a[i] != b[i] {
				return false
			}
		}
		opened, err := openEnvelope(a, key, "reconciliation", "ab010101010101010101010101010101")
		if err != nil {
			return false
		}
		s, _ := asString(opened["schema"])
		return s == proofSchema
	}, &passed, &failed)

	runCase(&log, "sanitize-redacts-paths", func() bool {
		a := sanitizeText(`cannot start worker (open C:\tools\python.exe: denied)`)
		if strings.Contains(a, `C:\tools`) {
			return false
		}
		if !strings.Contains(a, redactedPath) {
			return false
		}
		b := sanitizeText(`key path is on \\127.0.0.1\share\lan-queue.key`)
		if strings.Contains(b, `127.0.0.1`) {
			return false
		}
		if !strings.Contains(b, redactedPath) {
			return false
		}
		c := sanitizeText("result failed authentication (bad mac)")
		if strings.Contains(c, redactedPath) {
			return false
		}
		return c == "result failed authentication (bad mac)"
	}, &passed, &failed)

	runCase(&log, "strict-base64-rejects-whitespace", func() bool {
		key := testKey(0)
		good := base64.StdEncoding.EncodeToString(key)
		if _, err := strictBase64Decode(good); err != nil {
			return false
		}
		if _, err := strictBase64Decode(good + "\n"); err == nil {
			return false
		}
		if len(good) < 8 {
			return false
		}
		tampered := good[:4] + " " + good[4:]
		if _, err := strictBase64Decode(tampered); err == nil {
			return false
		}
		payload := map[string]any{
			"kind": "job", "id": "ef0123456789abcdef0123456789abcdef",
		}
		sealed, err := seal(payload, key, "job", "ef0123456789abcdef0123456789abcdef")
		if err != nil {
			return false
		}
		// Inject a newline into payload_b64: canonical open must refuse,
		// matching the bridge (validate=True). Go StdEncoding would ignore it.
		var env map[string]any
		parsed, err := parseJSONBytes(sealed)
		if err != nil {
			return false
		}
		env, _ = asMap(parsed)
		if env == nil {
			return false
		}
		b64, _ := asString(env["payload_b64"])
		env["payload_b64"] = b64[:8] + "\n" + b64[8:]
		rewrapped, err := canonicalJSON(env)
		if err != nil {
			return false
		}
		_, err = openEnvelope([]byte(rewrapped), key, "job", "ef0123456789abcdef0123456789abcdef")
		return err != nil
	}, &passed, &failed)

	runCase(&log, "settle-exact-tamper-refused", func() bool {
		key := testKey(0)
		jobID := "ab010101010101010101010101010101"
		generation := "ab010101010101010101010101010101"
		owner := map[string]any{"instance": "fixture", "pid": jsonNum(4194303)}
		resources := []any{"exclusive:global"}
		envelopeSha := "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
		dir, err := os.MkdirTemp("", "settle-exact")
		if err != nil {
			return false
		}
		defer os.RemoveAll(dir)
		for _, sub := range []string{"results", "starts", "releases"} {
			if err := os.MkdirAll(filepath.Join(dir, sub), 0o755); err != nil {
				return false
			}
		}
		startPayload := map[string]any{
			"schema": startSchema, "kind": "start", "id": jobID,
			"generation": generation, "owner": owner,
			"child_pid":           jsonNum(4194302),
			"started_at":          "2026-01-01T00:00:01+00:00",
			"job_envelope_sha256": envelopeSha, "resources": resources,
		}
		startRaw, err := seal(startPayload, key, "start", jobID)
		if err != nil {
			return false
		}
		if err := os.WriteFile(filepath.Join(dir, "starts", jobID+".json"), startRaw, 0o644); err != nil {
			return false
		}
		proofPayload := map[string]any{
			"schema": proofSchema, "kind": "reconciliation", "id": jobID,
			"claim_generation": generation, "job_envelope_sha256": envelopeSha,
			"resources": resources,
			"evidence": map[string]any{
				"schema": evidenceSchema,
				"owner":  owner, "child_pid": jsonNum(4194302),
				"tree_absent": true, "observer": "self-test",
				"observed_at": "2026-01-01T00:00:02+00:00",
			},
		}
		proofRaw, err := seal(proofPayload, key, "reconciliation", jobID)
		if err != nil {
			return false
		}
		proofPath := filepath.Join(dir, "proof.json")
		if err := os.WriteFile(proofPath, proofRaw, 0o644); err != nil {
			return false
		}
		start, err := openEnvelope(startRaw, key, "start", jobID)
		if err != nil {
			return false
		}
		proof, err := openEnvelope(proofRaw, key, "reconciliation", jobID)
		if err != nil {
			return false
		}
		expected, problem := expectedReconciliationResult(jobID, generation, start, proof, proofRaw)
		if problem != "" || expected == nil {
			return false
		}
		resultRaw, err := seal(expected, key, "result", jobID)
		if err != nil {
			return false
		}
		if err := os.WriteFile(filepath.Join(dir, "results", jobID+".json"), resultRaw, 0o644); err != nil {
			return false
		}
		releasePayload := map[string]any{
			"schema": "lenovo-job-release/1", "kind": "release", "id": jobID,
			"generation": generation, "owner": owner,
			"job_envelope_sha256": envelopeSha, "resources": resources,
			"result_envelope_sha256": sha256Hex(resultRaw),
		}
		releaseRaw, err := seal(releasePayload, key, "release", jobID)
		if err != nil {
			return false
		}
		if err := os.WriteFile(filepath.Join(dir, "releases", jobID+".json"), releaseRaw, 0o644); err != nil {
			return false
		}
		if problem, _, _ := verifySettlement(dir, proofPath, key, jobID, envelopeSha, resources, owner, generation); problem != "" {
			return false
		}
		// Authenticated tamper: re-seal with the same key but evil stdout.
		// Selected-field checks would still pass; exact comparison must refuse.
		tampered, err := openEnvelope(resultRaw, key, "result", jobID)
		if err != nil {
			return false
		}
		tampered["stdout"] = "evil"
		tamperedRaw, err := seal(tampered, key, "result", jobID)
		if err != nil {
			return false
		}
		if err := os.WriteFile(filepath.Join(dir, "results", jobID+".json"), tamperedRaw, 0o644); err != nil {
			return false
		}
		// Release still binds the old result digest, so either the result
		// mismatch or the release digest mismatch must refuse.
		problem, _, _ = verifySettlement(dir, proofPath, key, jobID, envelopeSha, resources, owner, generation)
		return problem != ""
	}, &passed, &failed)

	runCase(&log, "job-id-validation", func() bool {
		if !isHex32("ab010101010101010101010101010101") {
			return false
		}
		for _, bad := range []string{"", "not-hex", "AB010101010101010101010101010101",
			"ab01010101010101010101010101010", "ab0101010101010101010101010101011"} {
			if isHex32(bad) {
				return false
			}
		}
		return true
	}, &passed, &failed)

	runCase(&log, "worker-compat-accepts-current", func() bool {
		dir, err := os.MkdirTemp("", "worker-compat-ok")
		if err != nil {
			return false
		}
		defer os.RemoveAll(dir)
		workerPy := filepath.Join(dir, "worker.py")
		reconPy := filepath.Join(dir, "job_reconciliation.py")
		workerSrc := "WORKER_SCHEMA = \"lenovo-worker/1\"\n--reconcile-id --reconcile-proof\n"
		reconSrc := "PROOF_SCHEMA = \"lenovo-job-reconciliation/2\"\n" +
			"EVIDENCE_SCHEMA = \"lenovo-job-terminal-evidence/1\"\nDERIVATION = \"unobserved-crash\"\n"
		if err := os.WriteFile(workerPy, []byte(workerSrc), 0o644); err != nil {
			return false
		}
		if err := os.WriteFile(reconPy, []byte(reconSrc), 0o644); err != nil {
			return false
		}
		return checkWorkerCompatible(workerPy) == nil
	}, &passed, &failed)

	runCase(&log, "worker-compat-refuses-outdated", func() bool {
		dir, err := os.MkdirTemp("", "worker-compat-old")
		if err != nil {
			return false
		}
		defer os.RemoveAll(dir)
		workerPy := filepath.Join(dir, "worker.py")
		reconPy := filepath.Join(dir, "job_reconciliation.py")
		if err := os.WriteFile(workerPy, []byte("WORKER_SCHEMA = \"lenovo-worker/1\"\n--reconcile-id --reconcile-proof\n"), 0o644); err != nil {
			return false
		}
		if err := os.WriteFile(reconPy, []byte("PROOF_SCHEMA = \"lenovo-job-reconciliation/1\"\n"), 0o644); err != nil {
			return false
		}
		r := checkWorkerCompatible(workerPy)
		if r == nil || r.Reason != "worker-incompatible" {
			return false
		}
		missing := filepath.Join(dir, "absent-worker.py")
		if r := checkWorkerCompatible(missing); r == nil || r.Reason != "worker-missing" {
			return false
		}
		return true
	}, &passed, &failed)

	fmt.Printf("{\"schema\":\"lenovo-job-recovery-self-test/1\",\"passed\":%d,\"failed\":%d}\n", passed, failed)
	fmt.Fprint(os.Stderr, log.String())
	if failed != 0 {
		return 2
	}
	return 0
}

func runCase(log *strings.Builder, name string, body func() bool, passed, failed *int) {
	ok := false
	detail := ""
	func() {
		defer func() {
			if r := recover(); r != nil {
				ok = false
				detail = fmt.Sprintf(" (%v)", r)
			}
		}()
		ok = body()
	}()
	if ok {
		*passed++
		log.WriteString("ok " + name + "\n")
	} else {
		*failed++
		log.WriteString("NOT OK " + name + detail + "\n")
	}
}

func testKey(offset byte) []byte {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i) + offset
	}
	return key
}

func testKeyReversed() []byte {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(31 - i)
	}
	return key
}

func testKeyMul(m byte) []byte {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i) * m
	}
	return key
}

func jsonNum(n int64) any { return n }
