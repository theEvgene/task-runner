// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Installed worker compatibility: the exe never silently relies on an
// outdated worker checkout. The invoke path requires the canonical
// worker.py reconcile entry plus the v2 typed proof markers.
package main

import (
	"os"
	"path/filepath"
	"strings"
)

func checkWorkerCompatible(workerPy string) *Refusal {
	if workerPy == "" || !isFile(workerPy) {
		return refuse("worker-missing",
			"set workerDir (or --worker) at the canonical checkout",
			"canonical worker.py not found")
	}
	workerText, err := os.ReadFile(workerPy)
	if err != nil {
		return refuse("worker-incompatible",
			"install the matching worker checkout (copy scripts/lenovo_worker from the same revision as this exe, then re-run)",
			"installed worker.py is unreadable")
	}
	worker := string(workerText)
	for _, marker := range []string{"--reconcile-id", "--reconcile-proof", "lenovo-worker/1"} {
		if !strings.Contains(worker, marker) {
			return refuse("worker-incompatible",
				"install the matching worker checkout (copy scripts/lenovo_worker from the same revision as this exe, then re-run)",
				"installed worker lacks the reconcile entry")
		}
	}
	reconPath := filepath.Join(filepath.Dir(workerPy), "job_reconciliation.py")
	reconText, err := os.ReadFile(reconPath)
	if err != nil {
		return refuse("worker-incompatible",
			"install the matching worker checkout (copy scripts/lenovo_worker from the same revision as this exe, then re-run)",
			"installed job_reconciliation.py is unreadable")
	}
	recon := string(reconText)
	for _, marker := range []string{"lenovo-job-reconciliation/2", "lenovo-job-terminal-evidence/1", "unobserved-crash"} {
		if !strings.Contains(recon, marker) {
			return refuse("worker-incompatible",
				"install the matching worker checkout (copy scripts/lenovo_worker from the same revision as this exe, then re-run)",
				"installed worker proof schema is outdated")
		}
	}
	return nil
}
