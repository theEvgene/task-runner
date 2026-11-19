// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Evidence-gated recovery for one retained Lenovo queue job.
// See docs/lenovo-job-recovery.md for the operator contract.
package main

import (
	"fmt"
	"os"
	"path/filepath"
)

type options struct {
	configPath        string
	jobID             string
	jobIDGiven        bool
	proofOut          string
	queueOverride     string
	keyFileOverride   string
	keyFormatOverride string
	workerOverride    string
	pythonOverride    string
	observer          string
	invoke            bool
	verifyOnly        bool
	selfTest          bool
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(argv []string) int {
	o := &options{observer: "lenovo-recovery-exe/1 human-supervised"}
	for i := 0; i < len(argv); i++ {
		a := argv[i]
		next := func() (string, bool) {
			if i+1 >= len(argv) {
				return "", false
			}
			i++
			return argv[i], true
		}
		var v string
		var ok bool
		switch a {
		case "--config":
			if v, ok = next(); !ok {
				return usageErr("missing value for --config")
			}
			o.configPath = v
		case "--job-id":
			if v, ok = next(); !ok {
				return usageErr("missing value for --job-id")
			}
			o.jobID = v
			o.jobIDGiven = true
		case "--proof-out":
			if v, ok = next(); !ok {
				return usageErr("missing value for --proof-out")
			}
			o.proofOut = v
		case "--queue":
			if v, ok = next(); !ok {
				return usageErr("missing value for --queue")
			}
			o.queueOverride = v
		case "--key-file":
			if v, ok = next(); !ok {
				return usageErr("missing value for --key-file")
			}
			o.keyFileOverride = v
		case "--key-format":
			if v, ok = next(); !ok {
				return usageErr("missing value for --key-format")
			}
			o.keyFormatOverride = v
		case "--worker":
			if v, ok = next(); !ok {
				return usageErr("missing value for --worker")
			}
			o.workerOverride = v
		case "--python":
			if v, ok = next(); !ok {
				return usageErr("missing value for --python")
			}
			o.pythonOverride = v
		case "--observer":
			if v, ok = next(); !ok {
				return usageErr("missing value for --observer")
			}
			o.observer = v
		case "--invoke":
			o.invoke = true
		case "--verify-only":
			o.verifyOnly = true
		case "--self-test":
			o.selfTest = true
		case "--help", "-h", "/?":
			printUsage()
			return 0
		default:
			fmt.Fprintln(os.Stderr, "unknown argument: "+a)
			printUsage()
			return 2
		}
	}

	if o.selfTest {
		return selfTest()
	}

	// F3: --job-id is required. There is no default: an omitted flag must
	// refuse before any config, queue, or key access, never act on a
	// baked-in real id.
	if !o.jobIDGiven || !isHex32(o.jobID) {
		emitRefusal(o.jobID, "job-id-invalid",
			"pass one 32-lowercase-hex --job-id (required; no default)", "job id is not 32 lowercase hex")
		return 2
	}

	if o.configPath == "" {
		localAppData := os.Getenv("LOCALAPPDATA")
		if localAppData == "" {
			emitRefusal(o.jobID, "config-unreadable",
				"set %LOCALAPPDATA% or pass --config", "LOCALAPPDATA is not set")
			return 2
		}
		o.configPath = filepath.Join(localAppData, "task-runner", "lenovo-worker.json")
	}

	cfg, err := loadConfig(o.configPath, o)
	if err != nil {
		return emitErr(o.jobID, err)
	}
	if o.proofOut == "" {
		o.proofOut = filepath.Join(os.TempDir(), "lenovo-recovery", o.jobID+".proof.json")
	}
	return runRecovery(cfg, o.jobID, o.proofOut, o.observer, o.invoke, o.verifyOnly)
}

func usageErr(detail string) int {
	fmt.Fprintln(os.Stderr, "argument error: "+detail)
	return 2
}

func printUsage() {
	fmt.Fprintln(os.Stderr, "LenovoJobRecovery -- evidence-gated recovery for one owned/unknown Lenovo job.")
	fmt.Fprintln(os.Stderr, "  --config PATH     local Lenovo config (default %LOCALAPPDATA%\\task-runner\\lenovo-worker.json)")
	fmt.Fprintln(os.Stderr, "  --job-id HEX32    required; no default (refuses when omitted)")
	fmt.Fprintln(os.Stderr, "  --proof-out PATH  where the signed proof envelope is written (outside the queue)")
	fmt.Fprintln(os.Stderr, "  --verify-only     authenticate + gates only; write no proof, spawn nothing")
	fmt.Fprintln(os.Stderr, "  --invoke          after safe proof, run canonical worker.py --reconcile + verify")
	fmt.Fprintln(os.Stderr, "  --self-test       fixture-only checks; touches no live queue and no real key")
	fmt.Fprintln(os.Stderr, "Overrides for fixture interop (testing only): --queue --key-file --key-format --worker --python.")
	fmt.Fprintln(os.Stderr, "Exits: 0 ready/reconciled; 2 refused/failed.")
}
