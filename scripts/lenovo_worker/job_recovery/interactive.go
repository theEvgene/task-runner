// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Interactive double-click mode: no arguments prompts once for the job
// ID, runs verification, then recovers only when allowed. Human text goes
// to stderr; stdout stays JSON receipts so automation parsing is unchanged.
package main

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const interactiveObserver = "lenovo-recovery-exe/1 human-supervised"

func runInteractive() int {
	reader := bufio.NewReader(os.Stdin)
	fmt.Fprintln(os.Stderr, "Lenovo job recovery -- one job only.")
	fmt.Fprintln(os.Stderr, "This verifies the job and recovers it only when it is safe.")
	fmt.Fprintln(os.Stderr, "No other job is touched. Nothing secret is printed.")
	fmt.Fprint(os.Stderr, "Job ID (32 lowercase hex): ")
	line, err := reader.ReadString('\n')
	if err != nil && len(line) == 0 {
		emitRefusal("", "job-id-invalid",
			"re-run and type one 32-lowercase-hex job id",
			"no job id was typed")
		fmt.Fprintln(os.Stderr, "NOT recovered: no job ID was typed. Nothing was changed.")
		pauseToRead(reader)
		return 2
	}
	jobID := strings.TrimSpace(line)
	if !isHex32(jobID) {
		emitRefusal(jobID, "job-id-invalid",
			"re-run and type one 32-lowercase-hex job id",
			"job id is not 32 lowercase hex")
		fmt.Fprintln(os.Stderr, "NOT recovered: that job ID is not 32 lowercase hex. Nothing was changed.")
		pauseToRead(reader)
		return 2
	}
	configPath := os.Getenv("LOCALAPPDATA")
	if configPath == "" {
		emitRefusal(jobID, "config-unreadable",
			"set %LOCALAPPDATA% or run with --config",
			"LOCALAPPDATA is not set")
		fmt.Fprintln(os.Stderr, "NOT recovered: Lenovo config location is unknown. Nothing was changed.")
		pauseToRead(reader)
		return 2
	}
	cfgPath := filepath.Join(configPath, "task-runner", "lenovo-worker.json")
	cfg, err := loadConfig(cfgPath, &options{})
	if err != nil {
		_ = emitErr(jobID, err)
		fmt.Fprintln(os.Stderr, "NOT recovered: the Lenovo config or key setup is unusable. Nothing was changed.")
		pauseToRead(reader)
		return 2
	}
	proofOut := filepath.Join(os.TempDir(), "lenovo-recovery", jobID+".proof.json")
	fmt.Fprintln(os.Stderr, "Step 1/2: verifying (no changes, no recovery yet)...")
	if code := runRecovery(cfg, jobID, proofOut, interactiveObserver, false, true); code != 0 {
		fmt.Fprintln(os.Stderr, "NOT recovered: verification failed, so recovery did not run.")
		fmt.Fprintln(os.Stderr, "Nothing was changed except possibly nothing; read the reason above.")
		pauseToRead(reader)
		return 2
	}
	fmt.Fprintln(os.Stderr, "Verification passed. Step 2/2: recovering this job only...")
	if code := runRecovery(cfg, jobID, proofOut, interactiveObserver, true, false); code != 0 {
		fmt.Fprintln(os.Stderr, "NOT recovered: fail-closed stop. No other job was touched.")
		fmt.Fprintln(os.Stderr, "Read the reason above; fix it and re-run for the same ID only.")
		pauseToRead(reader)
		return 2
	}
	fmt.Fprintln(os.Stderr, "RECOVERED: this job was reconciled to the truthful non-success (error/unknown).")
	fmt.Fprintln(os.Stderr, "Confirm with the bridge fetch for the same ID if needed.")
	pauseToRead(reader)
	return 0
}

func pauseToRead(reader *bufio.Reader) {
	fmt.Fprint(os.Stderr, "Press Enter to close... ")
	line, _ := reader.ReadString('\n')
	_ = line
}
