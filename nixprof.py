#!/usr/bin/env python3

from collections import defaultdict
import heapq
import re
import subprocess
import json
from typing import List, TextIO, Union
import networkx
import click
from tabulate import tabulate

def simulate(g: networkx.DiGraph, max_nproc: Union[int, None], keep_start: bool = False) -> networkx.DiGraph:
    g = g.copy()

    # task -> remaining dependencies
    outdeg = {v: d for v, d in g.out_degree() if d > 0}
    # priority queue of tasks to be started by start time from log (as a tiebreaker)
    ready = [(g.nodes[v]["start"], v) for v, d in g.out_degree() if d == 0]
    heapq.heapify(ready)
    # priority queue of currently running tasks by stop time
    running = [(0, None)]

    while running:
        t, u = heapq.heappop(running)
        if u:
            for v in g.predecessors(u):
                outdeg[v] -= 1
                if outdeg[v] == 0:
                    heapq.heappush(ready, (g.nodes[v]["start"], v))
        free_procs = set(range(max_nproc or len(running) + len(ready))) - set(g.nodes[v]["proc"] for _, v in running)
        for proc in sorted(list(free_procs))[:len(ready)]:
            if keep_start:
                if running and ready[0][0] > running[0][0]:
                    break
            _, v = heapq.heappop(ready)
            g.nodes[v]["proc"] = proc
            if not keep_start:
                g.nodes[v]["start"] = t
                g.nodes[v]["stop"] = t + g.nodes[v]["time"]
            heapq.heappush(running, (g.nodes[v]["stop"], v))

    return g

def write_chrome_trace(g: networkx.DiGraph, out: TextIO, crit_path: List[str]):
    def mk_event(d, tid=None):
        return {
            "name": d["drv_name"],
            "cat": "build",
            "ph": "X",
            "ts": d["start"] * 1000000,
            "dur": d["time"] * 1000000,
            "pid": 0,
            "tid": d["proc"] + 1 if tid is None else tid
        }
    trace_events = [{"name": "thread_name", "ph": "M", "pid":0, "tid":0, "args": {"name": "critical path"}}] +\
      [mk_event(d) for _, d in g.nodes(data=True)] +\
      [mk_event(g.nodes[v], tid=0) for v in crit_path]
    json.dump({"traceEvents": trace_events}, out)

@click.group()
def nixprof():
    pass

@nixprof.command(context_settings=dict(
    ignore_unknown_options=True,
))
@click.option("-o", "--out", default="nixprof.log", help="output filename", show_default=True)
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
def record(cmd, out):
    """Record timings of a `nix-build`/`nix build` invocation."""
    subprocess.run(f"\\time {' '.join(cmd)} --log-format internal-json 2>&1 | ts -s -m \"[%.s]\" > {out}", shell=True, check=True)

DOTFILE = "nixprof.dot"
CHROMEFILE = "nixprof.trace_event"

def parse(input):
    g = networkx.DiGraph(rankdir="BT")
    id_to_drv = {}
    for line in input.readlines():
        if m := re.match(r"\[([^]]*)\] @nix ", line):
            time = float(m.group(1))
            entry = json.loads(line[m.end(0):])
            if entry["action"] == "start" and entry["type"] == 105:  # 105 = actBuild
                drv = entry["fields"][0]
                id_to_drv[entry["id"]] = drv
                name = re.search(r"-(.*)\.drv", drv)[1]
                g.add_node(drv, drv_name=name, start=time)
            elif entry["action"] == "stop":
                if drv := id_to_drv.get(entry["id"]):
                    g.nodes[drv]["stop"] = time
                    g.nodes[drv]["time"] = time - g.nodes[drv]["start"]

    return (g, id_to_drv)

@nixprof.command()
@click.option("-i", "--in", "input", default="nixprof.log", help="log input filename", type=click.File('r'))
@click.option("-t", "--tred", help="remove transitive edges (can speed up and declutter dot graph display)", is_flag=True)
@click.option("-p", "--print-crit-path", help="print critical (longest) path", is_flag=True)
@click.option("-a", "--print-avg-crit", help="print average contribution to critical paths", is_flag=True)
@click.option("-s", "--print-sim-times", help="print simulated build times by processor count up to optimal count", is_flag=True)
@click.option("-d", "--save-dot", is_flag=False, flag_value=DOTFILE, help="write dot graph to file", type=click.File('w'))
@click.option("-c", "--save-chrome-trace", is_flag=False, flag_value=CHROMEFILE, help="write `chrome://tracing`'s `trace_event` format to file. When combined with `-s`, also write simulated traces to files with processor count as suffix.")
@click.option("--all", help="print all analyses, write all output files", is_flag=True)
@click.option("--merge-into-pred", help="for each derivation with exactly one predecessor (dependency) and whose name matches the given regex, merge build time and dependents into that predecessor")
@click.option("--merge-into-succ", help="for each derivation with exactly one successor (dependent) and whose name matches the given regex, merge build time and dependencies into that successor")
def report(input: TextIO, tred, print_crit_path, print_avg_crit, print_sim_times, save_dot, save_chrome_trace, all, merge_into_pred, merge_into_succ):
    """Report various metrics of a recorded log."""
    g, id_to_drv = parse(input)

    drv_data = json.loads(subprocess.run(["nix", "--experimental-features", "nix-command", "path-info", "--json", "--derivation"] + list(g), capture_output=True, check=True).stdout)
    for d in drv_data:
        for dep in d["references"]:
            if dep in g:
                g.add_edge(d["path"], dep)

    if tred:
        g2: networkx.DiGraph = networkx.transitive_reduction(g)
        g2.update(nodes=g.nodes(data=True))
        g = g2

    if merge_into_pred:
        pat = re.compile(merge_into_pred)
        for drv in list(g):
            if pat.search(drv):
                # uhh, maybe I should really invert the graph...
                preds = list(g.successors(drv))
                if len(preds) == 1:
                    g.nodes[preds[0]]["time"] += g.nodes[drv]["time"]
                    networkx.contracted_nodes(g, preds[0], drv, self_loops=False, copy=False)
                    del g.nodes[preds[0]]["contraction"]

    if merge_into_succ:
        pat = re.compile(merge_into_succ)
        for drv in list(g):
            if pat.search(drv):
                succs = list(g.predecessors(drv))
                if len(succs) == 1:
                    g.nodes[succs[0]]["time"] += g.nodes[drv]["time"]
                    networkx.contracted_nodes(g, succs[0], drv, self_loops=False, copy=False)
                    del g.nodes[succs[0]]["contraction"]

    if tred:
        g2: networkx.DiGraph = networkx.transitive_reduction(g)
        g2.update(nodes=g.nodes(data=True))
        g = g2

    # copy time from nodes to outgoing edges for `dag_longest_path`
    for u, v, data in g.edges(data=True):
        data["time"] = g.nodes[u]["time"]

    crit_path: List[str] = list(reversed(networkx.dag_longest_path(g, weight="time")))
    if print_crit_path or all:
        print("Critical path")
        max_time = sum([g.nodes[u]["time"] for u in crit_path])
        cum_time = 0
        tab = []
        for u in crit_path:
            time = g.nodes[u]["time"]
            cum_time += time
            tab.append((time, time / max_time, cum_time, cum_time / max_time, g.nodes[u]["drv_name"]))
        print(tabulate(tab, headers=["time [s]", "", "[cum]", "", "drv"], floatfmt=[".1f", ".1%", ".1f", ".1%"]))
        print()

    if print_avg_crit or all:
        avg_contrib = defaultdict(lambda: 0)
        for u in g.nodes:
            ug = networkx.subgraph(g, networkx.ancestors(g, u))
            for v in networkx.dag_longest_path(ug, weight="time"):
                avg_contrib[v] += g.nodes[v]["time"] / len(g.nodes)

        print("Average contribution to critical paths")
        total_contrib = sum(avg_contrib.values())
        cum_contrib = 0
        tab = []
        for u, t in list(sorted(avg_contrib.items(), key=(lambda p: p[1]), reverse=True)):
            if t < 0.05:
                tab.append((0, 0, total_contrib, 1, "[total]"))
                break
            cum_contrib += t
            tab.append((t, t / total_contrib, cum_contrib, cum_contrib / total_contrib, g.nodes[u]["drv_name"]))
        print(tabulate(tab, headers=["time [s]", "", "[cum]", "", "drv"], floatfmt=[".1f", ".1%", ".1f", ".1%"]))
        print()

    if print_sim_times or all:
        print("Simulated build times by processor count up optimal power of two")
        cum_time = sum(d["time"] for _, d in g.nodes(data=True))
        tab = []
        nproc = 1
        prev_time = None
        while True:
            gs = simulate(g, max_nproc=nproc)
            time = max(d["stop"] for _, d in gs.nodes(data=True))
            if time == prev_time:
                break
            prev_time = time
            tab.append((nproc, time, cum_time / time))
            if save_chrome_trace or all:
                write_chrome_trace(gs, open(f"{save_chrome_trace or CHROMEFILE}.{nproc}", 'w'), crit_path)
            nproc *= 2
        print(tabulate(tab, headers=["#CPUs", "time [s]", "CPU% [avg]"], floatfmt=["", "f", ".0%"]))
        print()

    if save_dot or all:
        for u in g.nodes:
            name = g.nodes[u]["drv_name"]
            time = g.nodes[u]["time"]
            g.nodes[u]["label"] = f"{name}\\n{time:.1f}s"
            g.nodes[u]["height"] = time
            g.nodes[u]["shape"] = "box"

        for drv in crit_path:
            g.nodes[drv]["color"] = "red"

        for i in range(len(crit_path) - 1):
            g[crit_path[i+1]][crit_path[i]]["color"] = "red"

        networkx.nx_pydot.write_dot(g, save_dot or DOTFILE)

    if save_chrome_trace or all:
        g_sim = simulate(g, max_nproc=None, keep_start=True)
        write_chrome_trace(g_sim, open(save_chrome_trace or CHROMEFILE, 'w'), crit_path)

@nixprof.command()
@click.argument("base", type=click.File('r'))
@click.argument("curr", default="nixprof.log", type=click.File('r'))
@click.option("-m", "--matching", help="report only derivations whose names match the given regex")
def diff(base, curr, matching):
    """Report timing differences between two recorded logs."""
    base, _ = parse(base)
    base = { base.nodes[drv]["drv_name"] : base.nodes[drv]["time"] for drv in base }
    curr, _ = parse(curr)
    curr = { curr.nodes[drv]["drv_name"] : curr.nodes[drv]["time"] for drv in curr }

    drvs = set(base.keys()) | set(curr.keys())
    if matching:
        pat = re.compile(matching)
        drvs = { drv for drv in drvs if pat.search(drv) }

    diffs = [(curr.get(drv, 0) - base.get(drv, 0), drv) for drv in drvs]
    diffs = [(d, d / base.get(drv, d), drv) for d, drv in diffs]
    diffs.sort(key=lambda d: -abs(d[0]))
    s = sum(d[0] for d in diffs)
    diffs = [d for d in diffs if abs(d[0]) > 0.001]
    diffs.append((s, s / sum(base[drv] for drv in base), "total"))
    print(tabulate(diffs, headers=["diff [s]", "", "drv"], floatfmt=["+.3g", "+.1%"]))
    print()

if __name__ == '__main__':
    nixprof()
