// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Single-JSON-line receipts on stdout. Receipts carry only digests, pids,
// and reasons: no key, no password, no host, no absolute path. The owner
// instance is a config-local worker name bound to the claim owner.
package main

import (
	"fmt"
	"os"
)

const recoverySchema = "lenovo-job-recovery/1"

func emitJSONLine(doc map[string]any) {
	text, err := canonicalJSON(doc)
	if err != nil {
		text = `{"schema":"` + recoverySchema + `"}`
	}
	fmt.Println(text)
}

func emitReady(jobID, envelopeSha, resourcesDigest string, ownerPid, childPid int64, ownerInstance, proofSha string, settled bool) {
	outcome := "ready-to-reconcile"
	next := "invoke canonical worker --reconcile with the written proof"
	if settled {
		outcome = "reconciled"
		next = "none"
	}
	evidence := map[string]any{
		"jobEnvelopeSha256": envelopeSha,
		"resourcesDigest":   resourcesDigest,
		"ownerPid":          num(ownerPid),
		"ownerInstance":     ownerInstance,
		"childPid":          num(childPid),
		"treeAbsent":        true,
	}
	if proofSha != "" {
		evidence["proofEnvelopeSha256"] = proofSha
	}
	emitJSONLine(map[string]any{
		"schema":     recoverySchema,
		"jobId":      jobID,
		"outcome":    outcome,
		"reason":     "none",
		"nextAction": next,
		"evidence":   evidence,
	})
}

func emitReconciled(jobID, envelopeSha, resourcesDigest string, ownerPid, childPid int64, ownerInstance, resultSha, releaseSha string, replay bool) {
	emitJSONLine(map[string]any{
		"schema":     recoverySchema,
		"jobId":      jobID,
		"outcome":    "reconciled",
		"reason":     "none",
		"nextAction": "none",
		"evidence": map[string]any{
			"jobEnvelopeSha256":     envelopeSha,
			"resourcesDigest":       resourcesDigest,
			"ownerPid":              num(ownerPid),
			"ownerInstance":         ownerInstance,
			"childPid":              num(childPid),
			"treeAbsent":            true,
			"resultEnvelopeSha256":  resultSha,
			"releaseEnvelopeSha256": releaseSha,
			"replay":                replay,
		},
	})
}

func emitRefusal(jobID, reason, nextAction, detail string) {
	detail = sanitizeText(detail)
	nextAction = sanitizeText(nextAction)
	reason = sanitizeText(reason)
	emitJSONLine(map[string]any{
		"schema":     recoverySchema,
		"jobId":      jobID,
		"outcome":    "refused",
		"reason":     reason,
		"nextAction": nextAction,
		"evidence":   map[string]any{"detail": detail},
	})
	fmt.Fprintln(os.Stderr, "recovery refused ("+reason+"): "+detail)
}

// num renders an integer for canonical output without float semantics.
func num(n int64) any { return n }
