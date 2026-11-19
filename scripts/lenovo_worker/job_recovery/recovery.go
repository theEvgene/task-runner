// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Evidence-gated recovery for one retained queue job. Performs no queue
// mutation itself: the only writer is the canonical worker, invoked solely
// after every fail-closed gate passes.
package main

import (
	"os"
	"path/filepath"
	"strconv"
)

type config struct {
	QueueDir  string
	SharedDir string
	KeyFile   string
	KeyFormat string
	WorkerPy  string
	PythonExe string
}

func loadConfig(configPath string, o *options) (*config, error) {
	text, err := os.ReadFile(configPath)
	if err != nil {
		return nil, refuse("config-unreadable",
			"point --config at the real Lenovo JSON", "cannot read config")
	}
	parsed, err := parseJSONBytes(text)
	if err != nil {
		return nil, refuse("config-unreadable",
			"fix the JSON syntax of the Lenovo config", "config is not valid JSON")
	}
	cfg, ok := asMap(parsed)
	if !ok {
		return nil, refuse("config-invalid",
			"restore the Lenovo config object", "config must be a JSON object")
	}
	out := &config{}
	out.QueueDir = optString(o.queueOverride, strField(cfg, "queueDir"))
	out.SharedDir = strField(cfg, "sharedDir")
	out.KeyFile = optString(o.keyFileOverride, strField(cfg, "keyFile"))
	out.KeyFormat = optString(o.keyFormatOverride, strField(cfg, "keyFormat"))
	if out.KeyFormat == "" {
		out.KeyFormat = "raw"
	}
	workerDir := strField(cfg, "workerDir")
	if o.workerOverride != "" {
		out.WorkerPy = o.workerOverride
	} else if workerDir != "" {
		out.WorkerPy = filepath.Join(workerDir, "worker.py")
	}
	python := strField(cfg, "python")
	if o.pythonOverride != "" {
		python = o.pythonOverride
	}
	out.PythonExe = python
	if out.QueueDir == "" || !filepath.IsAbs(out.QueueDir) {
		return nil, refuse("config-invalid",
			"set queueDir to an absolute path (UNC or mapped share allowed)",
			"queueDir must be an absolute path")
	}
	if out.KeyFile == "" || !filepath.IsAbs(out.KeyFile) {
		return nil, refuse("config-invalid",
			"set keyFile to an absolute local path", "keyFile must be an absolute path")
	}
	if out.KeyFormat != "raw" && out.KeyFormat != "base64" {
		return nil, refuse("config-invalid",
			"set keyFormat to raw or base64", "keyFormat must be raw or base64")
	}
	if out.SharedDir != "" && !filepath.IsAbs(out.SharedDir) {
		return nil, refuse("config-invalid",
			"set sharedDir to an absolute path (UNC or mapped share allowed) or omit it",
			"sharedDir must be an absolute path")
	}
	return out, nil
}

func strField(cfg map[string]any, name string) string {
	s, _ := asString(cfg[name])
	return s
}

func optString(override, base string) string {
	if override != "" {
		return override
	}
	return base
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

func shortErr(err error) string {
	if err == nil {
		return "unexpected failure"
	}
	m := err.Error()
	if len(m) > 160 {
		m = m[:160]
	}
	// F2: error text comes from the OS and can embed absolute paths
	// (config, key, proof, interpreter). Redact before it reaches a
	// receipt or stderr.
	return sanitizeText(m)
}

func runRecovery(cfg *config, jobID, proofOut, observer string, invoke, verifyOnly bool) int {
	// The queue must already exist; this tool never creates it.
	if !isDir(filepath.Join(cfg.QueueDir, "jobs")) || !isDir(filepath.Join(cfg.QueueDir, "results")) {
		emitRefusal(jobID, "queue-missing",
			"check path/mount; the queue is never created",
			"queue/jobs or queue/results is not a directory")
		return 2
	}
	key, rerr := custodyGate(cfg, proofOut)
	if rerr != nil {
		emitRefusal(jobID, rerr.Reason, rerr.NextAction, rerr.Detail)
		return 2
	}
	defer clearKey(key)

	jobRaw, err := readQueueFile(cfg.QueueDir, "jobs", jobID, "job-unverifiable")
	if err != nil {
		return emitErr(jobID, err)
	}
	claimRaw, err := readQueueFile(cfg.QueueDir, "claims", jobID, "claim-invalid")
	if err != nil {
		return emitErr(jobID, err)
	}
	startRaw, err := readQueueFile(cfg.QueueDir, "starts", jobID, "no-durable-start")
	if err != nil {
		return emitErr(jobID, err)
	}

	if _, err := openEnvelope(jobRaw, key, "job", jobID); err != nil {
		emitRefusal(jobID, "job-unverifiable",
			"do not reconcile; the job envelope failed authentication",
			"job envelope refused ("+shortErr(err)+")")
		return 2
	}
	claim, err := openEnvelope(claimRaw, key, "claim", jobID)
	if err != nil {
		emitRefusal(jobID, "claim-invalid",
			"do not reconcile; the claim failed authentication",
			"claim envelope refused ("+shortErr(err)+")")
		return 2
	}
	start, err := openEnvelope(startRaw, key, "start", jobID)
	if err != nil {
		emitRefusal(jobID, "no-durable-start",
			"reconcile only with a durable start record",
			"start envelope refused ("+shortErr(err)+")")
		return 2
	}

	envelopeSha := sha256Hex(jobRaw)
	if err := validateClaim(claim, jobID, envelopeSha); err != nil {
		return emitErr(jobID, err)
	}
	if err := validateStart(start, claim, jobID, envelopeSha); err != nil {
		return emitErr(jobID, err)
	}

	owner, _ := claimOwner(claim)
	ownerPid, _ := asInt(owner["pid"])
	ownerInstance, _ := asString(owner["instance"])
	childPid, _ := asInt(start["child_pid"])
	resources, _ := asList(claim["resources"])
	generation, _ := asString(claim["generation"])

	// Owner must be dead: reconcile only the retained claim afterwards.
	ownerAlive, err := isAlive(ownerPid)
	if err != nil {
		emitRefusal(jobID, "inventory-unavailable",
			"re-run; never assume the owner is gone", "owner liveness unobservable")
		return 2
	}
	if ownerAlive {
		emitRefusal(jobID, "owner-active",
			"stop that worker first (its pid dies with it), then re-run",
			"claim owner pid "+itoa(ownerPid)+" still active")
		return 2
	}

	// Child tree must be proven absent; unavailability refuses.
	treeAlive, treeDetail, err := claimedTreeAlive(childPid)
	if err != nil {
		emitRefusal(jobID, "inventory-unavailable",
			"re-run; never assume the tree is gone", "tree inventory failed ("+shortErr(err)+")")
		return 2
	}
	if treeAlive {
		emitRefusal(jobID, "tree-active",
			"wait for the tree to exit and re-run; never delete or resubmit the job",
			"claimed child tree still active ("+treeDetail+")")
		return 2
	}

	resText, err := canonicalJSON(resources)
	if err != nil {
		return emitErr(jobID, err)
	}
	resourcesDigest := sha256Hex([]byte(resText))

	if verifyOnly {
		emitReady(jobID, envelopeSha, resourcesDigest, ownerPid, childPid, ownerInstance, "", false)
		return 0
	}

	if !invoke {
		proofSha, err := writeFreshProof(proofOut, observer, claim, ownerInstance,
			ownerPid, childPid, envelopeSha, resources, key, jobID)
		if err != nil {
			return emitErr(jobID, err)
		}
		emitReady(jobID, envelopeSha, resourcesDigest, ownerPid, childPid, ownerInstance, proofSha, false)
		return 0
	}

	// Idempotent accept first: a verified settlement binding this exact
	// generation reports replay without spawning or minting a new proof.
	// The proof file must exist: replay re-validates the exact result
	// against the signed proof, never from the result alone.
	if resultSha, releaseSha, ok := settledAlready(cfg.QueueDir, proofOut, key, jobID,
		envelopeSha, resources, owner, generation); ok {
		emitReconciled(jobID, envelopeSha, resourcesDigest, ownerPid, childPid,
			ownerInstance, resultSha, releaseSha, true)
		return 0
	}

	// Reuse a still-valid proof so a repeated --invoke replays the
	// canonical idempotent path (same bytes: skipped + matched).
	if existing, err := os.ReadFile(proofOut); err == nil {
		if validateExistingProof(existing, key, jobID, generation,
			envelopeSha, resources, owner, childPid) != nil {
			if _, werr := writeFreshProof(proofOut, observer, claim, ownerInstance,
				ownerPid, childPid, envelopeSha, resources, key, jobID); werr != nil {
				return emitErr(jobID, werr)
			}
		}
	} else if _, werr := writeFreshProof(proofOut, observer, claim, ownerInstance,
		ownerPid, childPid, envelopeSha, resources, key, jobID); werr != nil {
		return emitErr(jobID, werr)
	}

	if cfg.WorkerPy == "" || !isFile(cfg.WorkerPy) {
		emitRefusal(jobID, "worker-missing",
			"set workerDir (or --worker) at the canonical checkout",
			"canonical worker.py not found")
		return 2
	}
	if cfg.PythonExe == "" || !isFile(cfg.PythonExe) {
		emitRefusal(jobID, "worker-missing",
			"set python (or --python) to the configured interpreter",
			"configured python not found")
		return 2
	}
	exit, err := invokeWorker(cfg.PythonExe, cfg, jobID, proofOut)
	if err != nil {
		return emitErr(jobID, err)
	}
	if exit != 0 {
		emitRefusal(jobID, "reconcile-failed",
			"read the worker stderr; change nothing and re-run only after the tree is proven absent",
			"canonical worker exited "+itoa(int64(exit)))
		return 2
	}
	problem, resultSha, releaseSha := verifySettlement(cfg.QueueDir, proofOut, key, jobID,
		envelopeSha, resources, owner, generation)
	if problem != "" {
		emitRefusal(jobID, "verify-failed",
			"keep the proof; re-read the same id, do not resubmit", problem)
		return 2
	}
	emitReconciled(jobID, envelopeSha, resourcesDigest, ownerPid, childPid,
		ownerInstance, resultSha, releaseSha, false)
	return 0
}

// custodyGate enforces key and proof custody before any envelope opens.
// The proof must be an absolute path outside the queue, the shared folder,
// and the checkout, and must not equal the key file (writing the proof must
// never be able to clobber the key).
func custodyGate(cfg *config, proofOut string) ([]byte, *Refusal) {
	keyFull := mustAbs(cfg.KeyFile)
	if remote, detail := isKeyPathRemoteOrUnprovable(cfg.KeyFile); remote {
		return nil, refuse("key-misplaced",
			"move the key to a local fixed disk outside the queue, share, and checkout",
			"key must stay local ("+detail+")")
	}
	queueFull := mustAbs(cfg.QueueDir)
	if withinOrEqual(keyFull, queueFull) {
		return nil, refuse("key-misplaced",
			"move the key outside the queue", "key inside the queue")
	}
	if cfg.SharedDir != "" {
		if withinOrEqual(keyFull, mustAbs(cfg.SharedDir)) {
			return nil, refuse("key-misplaced",
				"move the key outside the shared folder", "key inside the shared LAN folder")
		}
	}
	if root := checkoutRootFromWorkerPy(cfg.WorkerPy); root != "" {
		if withinOrEqual(keyFull, root) {
			return nil, refuse("key-misplaced",
				"move the key outside the checkout", "key inside the checkout")
		}
	}
	if withinOrEqual(mustAbs(proofOut), queueFull) {
		return nil, refuse("proof-misplaced",
			"choose a --proof-out outside the queue", "proof path inside the queue")
	}
	// Proof custody beyond the queue: the proof carries digests bound to a
	// live recovery and must not land on the LAN share, in the checkout
	// (commit/overwrite risk), or on top of the key file itself.
	if !filepath.IsAbs(proofOut) {
		return nil, refuse("proof-misplaced",
			"choose an absolute --proof-out outside the queue", "proof path is not absolute")
	}
	proofFull := mustAbs(proofOut)
	if cfg.SharedDir != "" {
		if withinOrEqual(proofFull, mustAbs(cfg.SharedDir)) {
			return nil, refuse("proof-misplaced",
				"choose a --proof-out outside the shared folder", "proof path inside the shared folder")
		}
	}
	if root := checkoutRootFromWorkerPy(cfg.WorkerPy); root != "" {
		if withinOrEqual(proofFull, root) {
			return nil, refuse("proof-misplaced",
				"choose a --proof-out outside the checkout", "proof path inside the checkout")
		}
	}
	if normalizedForCompare(proofFull) == normalizedForCompare(keyFull) {
		return nil, refuse("proof-misplaced",
			"choose a --proof-out that is not the key file", "proof path equals the key file")
	}
	key, err := loadKeyBytes(cfg.KeyFile, cfg.KeyFormat)
	if err != nil {
		if r, ok := err.(*Refusal); ok {
			return nil, r
		}
		return nil, refuse("key-unusable",
			"restore the key file named by the Lenovo config", "cannot read key file")
	}
	return key, nil
}

// settledAlready re-validates an existing settlement: a verified settlement
// binding this exact generation reports reconciled with replay true without
// spawning the worker or minting a new observation. The signed proof must be
// present: exact result comparison is derived from it.
func settledAlready(queueDir, proofPath string, key []byte, jobID, envelopeSha string, resources []any, owner map[string]any, generation string) (string, string, bool) {
	problem, resultSha, releaseSha := verifySettlement(queueDir, proofPath, key, jobID,
		envelopeSha, resources, owner, generation)
	if problem != "" {
		return "", "", false
	}
	return resultSha, releaseSha, true
}

func emitErr(jobID string, err error) int {
	if r, ok := err.(*Refusal); ok {
		emitRefusal(jobID, r.Reason, r.NextAction, r.Detail)
		return 2
	}
	emitRefusal(jobID, "unknown",
		"re-run; if it persists, stop and report this receipt",
		"unexpected failure ("+shortErr(err)+")")
	return 2
}

func isDir(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}

func isFile(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && !fi.IsDir()
}

func clearKey(key []byte) {
	for i := range key {
		key[i] = 0
	}
}
