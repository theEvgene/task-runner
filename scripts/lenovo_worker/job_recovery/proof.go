// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Typed proof: lenovo-job-reconciliation/2 carrying
// lenovo-job-terminal-evidence/1, bound to the claim generation, the exact
// signed job bytes, the exact resource set, the durable start owner, and
// the recorded child pid. Written outside the queue; the tool never seals
// any queue record itself.
package main

import (
	"os"
	"path/filepath"
	"time"
)

const (
	proofSchema    = "lenovo-job-reconciliation/2"
	evidenceSchema = "lenovo-job-terminal-evidence/1"
)

func writeFreshProof(proofOut, observer string, claim map[string]any, ownerInstance string, ownerPid, childPid int64, envelopeSha string, resources []any, key []byte, jobID string) (string, error) {
	observedAt := time.Now().UTC().Format("2006-01-02T15:04:05+00:00")
	gen, _ := asString(claim["generation"])
	proof := map[string]any{
		"schema":              proofSchema,
		"kind":                "reconciliation",
		"id":                  jobID,
		"claim_generation":    gen,
		"job_envelope_sha256": envelopeSha,
		"resources":           resources,
		"evidence": map[string]any{
			"schema":      evidenceSchema,
			"owner":       map[string]any{"instance": ownerInstance, "pid": ownerPid},
			"child_pid":   childPid,
			"tree_absent": true,
			"observer":    observer,
			"observed_at": observedAt,
		},
	}
	sealed, err := seal(proof, key, "reconciliation", jobID)
	if err != nil {
		return "", refuse("proof-write-failed",
			"re-run; the proof was not written", "cannot seal proof ("+shortErr(err)+")")
	}
	if reopened, err := openEnvelope(sealed, key, "reconciliation", jobID); err != nil {
		return "", refuse("proof-write-failed",
			"re-run; the proof was not written", "sealed proof failed self-check ("+shortErr(err)+")")
	} else if s, _ := asString(reopened["schema"]); s != proofSchema {
		return "", refuse("proof-write-failed",
			"re-run; the proof was not written", "sealed proof failed self-check (schema drift)")
	}
	if dir := filepath.Dir(proofOut); dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return "", refuse("proof-write-failed",
				"choose a writable --proof-out outside the queue",
				"cannot write proof ("+shortErr(err)+")")
		}
	}
	if err := os.WriteFile(proofOut, sealed, 0o644); err != nil {
		return "", refuse("proof-write-failed",
			"choose a writable --proof-out outside the queue",
			"cannot write proof ("+shortErr(err)+")")
	}
	return sha256Hex(sealed), nil
}

func validateExistingProof(existing, key []byte, jobID, generation, envelopeSha string, resources []any, owner map[string]any, childPid int64) error {
	proof, err := openEnvelope(existing, key, "reconciliation", jobID)
	if err != nil {
		return refuse("", "", "stored proof fails authentication ("+shortErr(err)+")")
	}
	if s, _ := asString(proof["schema"]); s != proofSchema {
		return refuse("", "", "stored proof schema is not "+proofSchema)
	}
	if g, _ := asString(proof["claim_generation"]); g != generation {
		return refuse("", "", "stored proof binds another generation")
	}
	if s, _ := asString(proof["job_envelope_sha256"]); s != envelopeSha {
		return refuse("", "", "stored proof binds another job envelope")
	}
	storedRes, ok := asList(proof["resources"])
	if !ok {
		return refuse("", "", "stored proof resources mismatch")
	}
	a, errA := canonicalJSON(storedRes)
	b, errB := canonicalJSON(resources)
	if errA != nil || errB != nil || a != b {
		return refuse("", "", "stored proof binds another resource set")
	}
	ev, ok := asMap(proof["evidence"])
	if !ok {
		return refuse("", "", "stored proof carries no typed evidence")
	}
	if s, _ := asString(ev["schema"]); s != evidenceSchema {
		return refuse("", "", "stored proof evidence is not typed")
	}
	evOwner, ok := asMap(ev["owner"])
	if !ok {
		return refuse("", "", "stored proof evidence names another owner")
	}
	c, errC := canonicalJSON(evOwner)
	d, errD := canonicalJSON(owner)
	if errC != nil || errD != nil || c != d {
		return refuse("", "", "stored proof evidence names another owner")
	}
	evChild, ok := asInt(ev["child_pid"])
	if !ok || evChild != childPid {
		return refuse("", "", "stored proof evidence names another tree")
	}
	treeAbsent, ok := ev["tree_absent"].(bool)
	if !ok || !treeAbsent {
		return refuse("", "", "stored proof does not assert tree_absent")
	}
	return nil
}
