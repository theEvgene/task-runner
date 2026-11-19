// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Settlement only via the canonical worker (scripts/lenovo_worker/
// worker.py --reconcile-id/--reconcile-proof), then re-validation exactly
// like the canonical release gate: the truthful non-success plus a fully
// bound lenovo-job-release/1.
package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

func invokeWorker(pythonExe string, cfg *config, jobID, proofPath string) (int, error) {
	args := []string{
		cfg.WorkerPy,
		"--queue", cfg.QueueDir,
		"--key-file", cfg.KeyFile,
		"--key-format", cfg.KeyFormat,
	}
	if cfg.SharedDir != "" {
		args = append(args, "--shared", cfg.SharedDir)
	}
	args = append(args,
		"--reconcile-id", jobID,
		"--reconcile-proof", proofPath,
	)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, pythonExe, args...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return 0, refuse("reconcile-failed",
			"fix the configured python/worker paths", "cannot start worker ("+shortErr(err)+")")
	}
	err := cmd.Wait()
	if ctx.Err() == context.DeadlineExceeded {
		return 0, refuse("reconcile-failed",
			"read the worker output; change nothing", "worker timed out")
	}
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			return exitErr.ExitCode(), nil
		}
		return 0, refuse("reconcile-failed",
			"read the worker output; change nothing", "worker failed ("+shortErr(err)+")")
	}
	return 0, nil
}

// reconcileStderr is the exact canonical stderr sealed by worker.py
// job_reconciliation.reconcile for the truthful non-success. The verifier
// below requires it byte-for-byte: a key holder can seal any bytes, so only
// the exact derivation counts as the observed crash outcome.
const reconcileStderr = "reconciled after worker crash: terminal exit, signal, and output were not observed; ownership released by proof, outcome is not success"

// verifySettlement re-validates the settlement exactly like the canonical
// release gate and exactly like the canonical reconcile result. "" means the
// settlement is fully bound; otherwise the returned problem names the
// mismatch for a verify-failed refusal.
//
// F1: the result is compared canonically exact against the expected
// reconciliation payload derived from the durable start (started_at), the
// signed proof (evidence observed_at, evidence digest, proof digest), and
// the claim bindings (generation). Selected-field checks are not enough: an
// authenticated tamper of stdout/stderr/finished_at/files/timed_out would
// otherwise still report replay true. Any deviation refuses.
func verifySettlement(queueDir, proofPath string, key []byte, jobID, envelopeSha string, resources []any, owner map[string]any, generation string) (problem, resultSha, releaseSha string) {
	resultRaw, err := os.ReadFile(filepath.Join(queueDir, "results", jobID+".json"))
	if err != nil {
		return "signed result missing after reconcile", "", ""
	}
	result, err := openEnvelope(resultRaw, key, "result", jobID)
	if err != nil {
		return "result failed authentication (" + shortErr(err) + ")", "", ""
	}
	startRaw, err := os.ReadFile(filepath.Join(queueDir, "starts", jobID+".json"))
	if err != nil {
		return "signed start missing for settlement check", "", ""
	}
	start, err := openEnvelope(startRaw, key, "start", jobID)
	if err != nil {
		return "start failed authentication (" + shortErr(err) + ")", "", ""
	}
	proofRaw, err := os.ReadFile(proofPath)
	if err != nil {
		return "signed proof missing for settlement check", "", ""
	}
	proof, err := openEnvelope(proofRaw, key, "reconciliation", jobID)
	if err != nil {
		return "proof failed authentication (" + shortErr(err) + ")", "", ""
	}
	childPid, _ := asInt(start["child_pid"])
	if verr := validateExistingProof(proofRaw, key, jobID, generation,
		envelopeSha, resources, owner, childPid); verr != nil {
		return "proof binding mismatch for settlement (" + sanitizeText(shortErr(verr)) + ")", "", ""
	}
	expected, problem := expectedReconciliationResult(jobID, generation, start, proof, proofRaw)
	if problem != "" {
		return problem, "", ""
	}
	gotText, errGot := canonicalJSON(result)
	wantText, errWant := canonicalJSON(expected)
	if errGot != nil || errWant != nil || gotText != wantText {
		return "result does not match canonical reconciliation (tampered or foreign)", "", ""
	}
	releaseRaw, err := os.ReadFile(filepath.Join(queueDir, "releases", jobID+".json"))
	if err != nil {
		return "signed release missing after reconcile", "", ""
	}
	release, err := openEnvelope(releaseRaw, key, "release", jobID)
	if err != nil {
		return "release failed authentication (" + shortErr(err) + ")", "", ""
	}
	if !checkString(release, "schema", "lenovo-job-release/1") ||
		!checkString(release, "kind", "release") ||
		!checkString(release, "id", jobID) {
		return "release schema/kind/id mismatch", "", ""
	}
	if g, _ := asString(release["generation"]); g != generation {
		return "release generation mismatch", "", ""
	}
	relOwner, ok := asMap(release["owner"])
	if !ok {
		return "release owner mismatch", "", ""
	}
	a, errA := canonicalJSON(relOwner)
	b, errB := canonicalJSON(owner)
	if errA != nil || errB != nil || a != b {
		return "release owner mismatch", "", ""
	}
	if !checkString(release, "job_envelope_sha256", envelopeSha) {
		return "release job digest mismatch", "", ""
	}
	relRes, ok := asList(release["resources"])
	if !ok {
		return "release resources mismatch", "", ""
	}
	c, errC := canonicalJSON(relRes)
	d, errD := canonicalJSON(resources)
	if errC != nil || errD != nil || c != d {
		return "release resources mismatch", "", ""
	}
	relResult, _ := asString(release["result_envelope_sha256"])
	if relResult != sha256Hex(resultRaw) {
		return "release result digest mismatch", "", ""
	}
	return "", sha256Hex(resultRaw), sha256Hex(releaseRaw)
}

// expectedReconciliationResult rebuilds the exact canonical result payload
// sealed by job_reconciliation.reconcile: fixed non-success derivation plus
// started_at from the durable start, finished_at/evidence binding from the
// signed proof, and digests over the canonical evidence/proof bytes. Any
// unprovable field is a problem (fail closed), never a default success.
func expectedReconciliationResult(jobID, generation string, start, proof map[string]any, proofRaw []byte) (map[string]any, string) {
	startedAt, _ := asString(start["started_at"])
	ev, ok := asMap(proof["evidence"])
	if !ok {
		return nil, "proof carries no typed evidence"
	}
	observedAt, _ := asString(ev["observed_at"])
	if observedAt == "" {
		return nil, "proof evidence carries no observed_at"
	}
	evText, err := canonicalJSON(ev)
	if err != nil {
		return nil, "proof evidence not canonical"
	}
	expected := map[string]any{
		"kind": "result", "id": jobID,
		"status": "error", "exit_code": nil,
		"stdout": "", "stderr": reconcileStderr,
		"started_at": startedAt, "finished_at": observedAt,
		"timed_out": false, "output_truncated": false,
		"files":       map[string]any{},
		"termination": "unknown",
		"reconciliation": map[string]any{
			"claim_generation":      generation,
			"evidence_sha256":       sha256Hex([]byte(evText)),
			"proof_envelope_sha256": sha256Hex(proofRaw),
			"outcome_derivation":    "unobserved-crash",
		},
	}
	return expected, ""
}
