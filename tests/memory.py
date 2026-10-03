"""Measure process memory separately from cgroup memory, which includes file cache."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time


def process_stat(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'ppid':int(fields[1]), 'ticks':int(fields[11])+int(fields[12]), 'start_ticks':fields[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def descendants(roots):
    parents = {}
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            value = process_stat(int(path.name))
            if value:
                parents[int(path.name)] = value['ppid']
    selected = set(roots)
    while True:
        expanded = selected | {pid for pid, parent in parents.items() if parent in selected}
        if expanded == selected:
            return sorted(selected)
        selected = expanded


def process_memory(pid):
    identity = process_stat(pid)
    if not identity:
        return None
    try:
        result = {'pid':pid, 'command':Path(f'/proc/{pid}/comm').read_text().strip(),
                  'start_ticks':identity['start_ticks']}
        for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
            pieces = line.split()
            if len(pieces) == 3 and pieces[2] == 'kB':
                result[pieces[0].rstrip(':')+'_kib'] = int(pieces[1])
        # Do not attribute memory or CPU to another process if this PID was reused.
        current = process_stat(pid)
        if current is None or current['start_ticks'] != identity['start_ticks']:
            return None
        return result
    except (FileNotFoundError, ProcessLookupError):
        return None


def systemd_properties(service):
    result = subprocess.run(['systemctl', 'show', service, '--property=MainPID',
        '--property=ControlGroup', '--property=MemoryCurrent', '--property=CPUUsageNSec',
        '--property=TasksCurrent'], check=True, text=True, capture_output=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def cgroup_for_pid(pid):
    for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines():
        if line.startswith('0::'):
            return Path('/sys/fs/cgroup')/line[3:].lstrip('/')
    return None


def cgroup_usage(path):
    if path is None or not path.exists():
        return None
    result = {'path':str(path)}
    for filename in ('memory.current', 'memory.peak', 'pids.current'):
        file = path/filename
        if file.exists():
            result[filename.replace('.', '_')] = int(file.read_text())
    for filename in ('memory.stat', 'cpu.stat'):
        file = path/filename
        if file.exists():
            result[filename.replace('.', '_')] = {
                name:int(value) for name, value in (line.split() for line in file.read_text().splitlines())}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--service', help='Systemd unit; default environment-orchestrator.service')
    source.add_argument('--pid', type=int, action='append', help='Process roots; repeat for separate process trees')
    parser.add_argument('--main-only', action='store_true', help='Exclude child processes')
    parser.add_argument('--interval', type=float, default=1, help='CPU sampling interval in seconds')
    parser.add_argument('--implementation', default='rust')
    parser.add_argument('--guests', default='not inspected', help='Describe guest state for comparable results')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.interval <= 0 or args.interval > 60:
        parser.error('--interval must be greater than zero and at most 60 seconds')
    properties = None
    if args.pid:
        roots = args.pid
        if any(pid <= 0 for pid in roots):
            parser.error('Process IDs must be positive')
        cgroup = cgroup_for_pid(roots[0])
    else:
        service = args.service or 'environment-orchestrator.service'
        properties = systemd_properties(service)
        roots = [int(properties['MainPID'])]
        if roots[0] == 0:
            parser.error('The selected service has no running main process')
        cgroup = Path('/sys/fs/cgroup')/properties['ControlGroup'].lstrip('/')
    selected = roots if args.main_only else descendants(roots)
    if any(pid <= 0 for pid in selected):
        parser.error('Process IDs must be positive')
    before = {pid:process_stat(pid) for pid in selected}
    cgroup_before = cgroup_usage(cgroup)
    started = time.monotonic()
    time.sleep(args.interval)
    elapsed = time.monotonic()-started
    after = {pid:process_stat(pid) for pid in selected}
    processes = [value for pid in selected if (value := process_memory(pid)) is not None]
    clock_ticks = os.sysconf('SC_CLK_TCK')
    sampled = [pid for pid in selected if before[pid] and after[pid]
        and before[pid]['start_ticks'] == after[pid]['start_ticks']]
    process_cpu_seconds = sum(after[pid]['ticks']-before[pid]['ticks'] for pid in sampled)/clock_ticks
    usage = cgroup_usage(cgroup)
    evidence = {'implementation':args.implementation, 'guests':args.guests,
        'measured_at':datetime.now(timezone.utc).isoformat(), 'processes':processes,
        'total_pss_kib':sum(value.get('Pss_kib', 0) for value in processes),
        'total_rss_kib':sum(value.get('Rss_kib', 0) for value in processes),
        'memory_current_bytes':usage.get('memory_current') if usage else None,
        'cgroup':usage, 'cpu_sample':{'wall_seconds':round(elapsed, 6),
            'process_cpu_seconds':round(process_cpu_seconds, 6),
            'process_cpu_percent_of_one_core':round(100*process_cpu_seconds/elapsed, 4),
            'sampled_pids':sampled, 'exited_or_reused_pids':[pid for pid in selected if pid not in sampled]},
        'notes':[
            'PSS divides shared mapped pages across processes. Summed RSS can count shared pages more than once.',
            'Cgroup memory.current includes anonymous memory, file cache, and kernel memory. It is not process PSS.',
            'A cgroup can include processes outside the selected PID trees, especially with --pid.',
            'CPU sampling covers the selected process identities; short-lived new processes can be absent from process totals.',
            'Process CPU uses kernel clock ticks. Short samples can show rounding differences from cgroup CPU accounting.',
        ]}
    if properties:
        evidence['systemd'] = properties
    if usage and cgroup_before:
        initial_cpu = cgroup_before.get('cpu_stat', {}).get('usage_usec')
        final_cpu = usage.get('cpu_stat', {}).get('usage_usec')
        if initial_cpu is not None and final_cpu is not None:
            cgroup_cpu_seconds = (final_cpu-initial_cpu)/1_000_000
            evidence['cpu_sample']['cgroup_cpu_seconds'] = round(cgroup_cpu_seconds, 6)
            evidence['cpu_sample']['cgroup_cpu_percent_of_one_core'] = round(100*cgroup_cpu_seconds/elapsed, 4)
    content = json.dumps(evidence, indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    print(content, end='')


if __name__ == '__main__':
    main()
