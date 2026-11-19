// Lenovo owned/unknown job recovery helper (stdlib-only Go).
// Fail-closed liveness: the claim-owner pid must be provably dead and the
// recorded child pid plus its transitive descendants must be absent.
// Unobservable pids, unavailable inventory, or a possibly resurrected pid
// refuse; absence is never inferred from unavailability.
//
// Owner liveness mirrors scripts/lenovo_worker/proc.py is_alive on Windows
// (tasklist CSV row). Descendants mirror job_tree.py _windows_process_table
// (Toolhelp32 snapshot, no powershell dependency).
package main

import (
	"context"
	"fmt"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"time"
	"unsafe"
)

// isAlive reports whether a process with this pid exists. Any execution
// or observation failure is an error: the caller must refuse, never assume
// death from unavailability. In particular a non-zero tasklist exit (error,
// missing binary, denied inventory) is an error, not proof of death:
// tasklist exits 0 with an INFO row when the pid is simply absent.
func isAlive(pid int64) (bool, error) {
	if pid <= 0 {
		return false, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "tasklist",
		"/FI", "PID eq "+strconv.FormatInt(pid, 10),
		"/FO", "CSV", "/NH").Output()
	if err != nil {
		if ctx.Err() != nil {
			return false, ctx.Err()
		}
		return false, err
	}
	return strings.Contains(string(out), `"`+strconv.FormatInt(pid, 10)+`"`), nil
}

type processEntry32 struct {
	Size            uint32
	Usage           uint32
	ProcessID       uint32
	DefaultHeapID   uintptr
	ModuleID        uint32
	Threads         uint32
	ParentProcessID uint32
	PriClassBase    int32
	Flags           uint32
	ExeFile         [260]uint16
}

var (
	procSnapshot = modKernel32.NewProc("CreateToolhelp32Snapshot")
	procFirst    = modKernel32.NewProc("Process32FirstW")
	procNext     = modKernel32.NewProc("Process32NextW")
	procClose    = modKernel32.NewProc("CloseHandle")
)

const th32SnapProcess = 0x00000002

// processTable maps pid -> ppid from a Toolhelp32 snapshot.
func processTable() (map[uint32]uint32, error) {
	snap, _, _ := procSnapshot.Call(th32SnapProcess, 0)
	if snap == 0 || snap == ^uintptr(0) {
		return nil, fmt.Errorf("process snapshot unavailable")
	}
	defer procClose.Call(snap)
	table := map[uint32]uint32{}
	var entry processEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	r, _, _ := procFirst.Call(snap, uintptr(unsafe.Pointer(&entry)))
	if r == 0 {
		return table, nil
	}
	for {
		table[entry.ProcessID] = entry.ParentProcessID
		entry.Size = uint32(unsafe.Sizeof(entry))
		r, _, _ := procNext.Call(snap, uintptr(unsafe.Pointer(&entry)))
		if r == 0 {
			return table, nil
		}
	}
}

func descendants(root uint32) ([]uint32, error) {
	table, err := processTable()
	if err != nil {
		return nil, err
	}
	children := map[uint32][]uint32{}
	for pid, ppid := range table {
		children[ppid] = append(children[ppid], pid)
	}
	var seen []uint32
	inSeen := map[uint32]bool{}
	stack := []uint32{root}
	for len(stack) > 0 {
		current := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		for _, child := range children[current] {
			if child != root && !inSeen[child] {
				inSeen[child] = true
				seen = append(seen, child)
				stack = append(stack, child)
			}
		}
	}
	sort.Slice(seen, func(i, j int) bool { return seen[i] < seen[j] })
	return seen, nil
}

// claimedTreeAlive reports whether the claimed tree may still be active.
// Fail closed true: unobservable child, unavailable inventory, or any
// descendant means live.
func claimedTreeAlive(childPid int64) (bool, string, error) {
	alive, err := isAlive(childPid)
	if err != nil {
		return true, "", err
	}
	if alive {
		return true, "child pid observable", nil
	}
	stragglers, err := descendants(uint32(childPid))
	if err != nil {
		return true, "", err
	}
	if len(stragglers) > 0 {
		shown := stragglers
		if len(shown) > 8 {
			shown = shown[:8]
		}
		parts := make([]string, len(shown))
		for i, p := range shown {
			parts[i] = strconv.FormatUint(uint64(p), 10)
		}
		return true, "descendants observable: " + strings.Join(parts, ","), nil
	}
	return false, "", nil
}
