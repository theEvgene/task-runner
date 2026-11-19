// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Part of task-runner scripts/lenovo_worker/job_recovery/; built by
// build-go.bat into one LenovoJobRecovery.exe.
// Canonical behavior mirrors scripts/lenovo_worker/job_reconciliation.py
// and docs/lenovo-worker-shortcut.md.
package main

// Refusal is a fail-closed stop: the receipt names the reason, the next
// action, and a short detail. Key material never appears in any of them.
type Refusal struct {
	Reason     string
	NextAction string
	Detail     string
}

func (r *Refusal) Error() string { return r.Reason + ": " + r.Detail }

func refuse(reason, nextAction, detail string) *Refusal {
	return &Refusal{Reason: reason, NextAction: nextAction, Detail: detail}
}
