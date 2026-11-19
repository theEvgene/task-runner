// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Claim/start binding validation. Mirrors scripts/lenovo_worker/
// job_ownership.py validate_claim/validate_start and the shared
// scripts/lan_bridge/job_claim.py predicate.
package main

import (
	"os"
	"path/filepath"
)

const (
	claimSchema = "lenovo-job-claim/1"
	startSchema = "lenovo-job-start/1"
	heldState   = "owned-unknown-until-signed-result"
)

func readQueueFile(queueDir, sub, jobID, reason string) ([]byte, error) {
	path := filepath.Join(queueDir, sub, jobID+".json")
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, refuse(reason,
			"reconcile only with all three envelopes present", sub+"/"+jobID+".json is missing")
	}
	if len(data) > maxEnvelopeBytes {
		return nil, refuse(reason,
			"do not reconcile oversized envelopes", sub+" envelope exceeds 2 MiB")
	}
	return data, nil
}

func checkString(dict map[string]any, name, want string) bool {
	v, ok := asString(dict[name])
	return ok && v == want
}

func claimOwner(claim map[string]any) (map[string]any, bool) {
	return asMap(claim["owner"])
}

func validateClaim(claim map[string]any, jobID, envelopeSha string) error {
	if !checkString(claim, "schema", claimSchema) ||
		!checkString(claim, "kind", "claim") ||
		!checkString(claim, "id", jobID) ||
		!checkString(claim, "state", heldState) {
		return refuse("claim-invalid",
			"do not reconcile foreign claims", "claim schema/kind/id/state mismatch")
	}
	gen, ok := asString(claim["generation"])
	if !ok || !isHex32(gen) {
		return refuse("claim-invalid",
			"do not reconcile foreign claims", "claim generation mismatch")
	}
	owner, ok := claimOwner(claim)
	if !ok {
		return refuse("claim-invalid",
			"do not reconcile foreign claims", "claim owner mismatch")
	}
	inst, ok := asString(owner["instance"])
	if !ok || inst == "" {
		return refuse("claim-invalid",
			"do not reconcile foreign claims", "claim owner mismatch")
	}
	pid, ok := asInt(owner["pid"])
	if !ok || pid <= 0 {
		return refuse("claim-invalid",
			"do not reconcile foreign claims", "claim owner pid mismatch")
	}
	if !checkString(claim, "job_envelope_sha256", envelopeSha) {
		return refuse("claim-binding-mismatch",
			"do not reconcile; the claim binds another job envelope",
			"claim job digest mismatch")
	}
	if _, ok := asList(claim["resources"]); !ok {
		return refuse("claim-invalid",
			"do not reconcile foreign claims", "claim resources mismatch")
	}
	return nil
}

func validateStart(start, claim map[string]any, jobID, envelopeSha string) error {
	if !checkString(start, "schema", startSchema) ||
		!checkString(start, "kind", "start") ||
		!checkString(start, "id", jobID) {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start schema/kind/id mismatch")
	}
	claimGen, _ := asString(claim["generation"])
	if !checkString(start, "generation", claimGen) {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start generation mismatch")
	}
	startOwner, ok := asMap(start["owner"])
	owner, _ := claimOwner(claim)
	if !ok || owner == nil {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start owner mismatch")
	}
	a, errA := canonicalJSON(startOwner)
	b, errB := canonicalJSON(owner)
	if errA != nil || errB != nil || a != b {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start owner mismatch")
	}
	child, ok := asInt(start["child_pid"])
	if !ok || child <= 0 {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start child pid mismatch")
	}
	if !checkString(start, "job_envelope_sha256", envelopeSha) {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start job digest mismatch")
	}
	startRes, ok := asList(start["resources"])
	claimRes, ok2 := asList(claim["resources"])
	if !ok || !ok2 {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start resources mismatch")
	}
	c, errC := canonicalJSON(startRes)
	d, errD := canonicalJSON(claimRes)
	if errC != nil || errD != nil || c != d {
		return refuse("start-mismatch",
			"reconcile only with the durable start for this claim",
			"start resources mismatch")
	}
	return nil
}
