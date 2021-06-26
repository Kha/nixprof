#!/usr/bin/env python3

from collections import defaultdict
import heapq
import re
import os
from typing import Any, Dict, Union
import networkx
import click
from networkx.drawing.nx_pydot import write_dot

def simulate(g: networkx.DiGraph, max_nproc: Union[int, None]) -> networkx.DiGraph:
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
            _, v = heapq.heappop(ready)
            g.nodes[v]["proc"] = proc
            g.nodes[v]["start"] = t
            g.nodes[v]["stop"] = t + g.nodes[v]["time"]
            heapq.heappush(running, (g.nodes[v]["stop"], v))

    return g

@click.group()
def nixprof():
    pass

@nixprof.command()
@click.option("-o", "--out", default="nixprof.log", help="log output filename")
@click.argument("cmd", nargs=-1, type=click.UNPROCESSED)
def record(cmd, out):
    os.system(f"\\time {' '.join(cmd)} --debug 2>&1 | ts -s -m \"[%.s]\" > {out}")

DOTFILE = "nixprof.dot"

@nixprof.command()
@click.option("-i", "--in", "input", default="nixprof.log", help="log input filename", type=click.File('r'))
@click.option("-t", "--tred", help="remove transitive edges", is_flag=True)
@click.option("-p", "--print-crit-path", help="print critical (longest) path", is_flag=True)
@click.option("-a", "--print-avg-crit", help="print average contribution to critical paths", is_flag=True)
@click.option("-s", "--print-sim-times", help="print simulated build times by processor count up to optimal count", is_flag=True)
@click.option("-d", "--save-dot", is_flag=False, flag_value=DOTFILE, help="write dot graph to file", type=click.File('w'))
@click.option("--all", help="print all analyses, write all output files", is_flag=True)
@click.option("--lean", is_flag=True, hidden=True)
def report(input, tred, print_crit_path, print_avg_crit, print_sim_times, save_dot, all, lean):
    log = input.read()
    g = networkx.DiGraph(rankdir="BT")
    stops = {}
    for stop, drv in re.findall(f"\[(.*)\] building of '(.*)!.*' from .drv file: build done", log):
        stops[drv] = float(stop)
    for start, drv in re.findall(r"\[([^]]*)\] building '(.*)'...", log):
        name = re.search(r"-(.*)\.drv", drv)[1]
        start = float(start)
        g.add_node(drv, drv_name=name, start=start, stop=stops[drv], time=stops[drv] - start)
    for drv, dep in re.findall(f"building of '(.*)!.*' from .drv file: waitee 'building of '(.*)!.*' from .drv file' done", log):
        if g.has_node(dep):
            g.add_edge(drv, dep)
            g.add_nodes_from

    if tred:
        g2: networkx.DiGraph = networkx.transitive_reduction(g)
        g2.update(nodes=g.nodes(data=True))
        g = g2

    if lean:
        for u, v in list(g.edges):
            if "depRoot" in g.nodes[v]["drv_name"]:
                networkx.contracted_nodes(g, u, v, self_loops=False, copy=False)

    # copy time from nodes to incoming edges for `dag_longest_path`
    for u, v, data in g.edges(data=True):
        data["time"] = g.nodes[u]["time"]

    if print_crit_path or all:
        print("Critical path")
        crit_path = networkx.dag_longest_path(g, weight="time")
        max_time = sum([g.nodes[u]["time"] for u in crit_path])
        cum_time = 0
        for u in reversed(crit_path):
            time = g.nodes[u]["time"]
            cum_time += time
            print("\t{:.1f}s\t{:.1%}\t{:.1f}s\t{:.1%}\t{}".format(time, time / max_time, cum_time, cum_time / max_time, g.nodes[u]["drv_name"]))

    if print_avg_crit or all:
        avg_contrib = defaultdict(lambda: 0)
        for u in g.nodes:
            ug = networkx.subgraph(g, networkx.ancestors(g, u))
            for v in networkx.dag_longest_path(ug, weight="time"):
                avg_contrib[v] += g.nodes[v]["time"] / len(g.nodes)

        print("Average contribution to critical paths")
        total_contrib = sum(avg_contrib.values())
        cum_contrib = 0
        for u, t in list(sorted(avg_contrib.items(), key=(lambda p: p[1]), reverse=True)):
            if t < 0.05:
                break
            cum_contrib += t
            print("\t{:.1f}s\t{:.1%}\t{:.1f}s\t{:.1%}\t{}".format(t, t / total_contrib, cum_contrib, cum_contrib / total_contrib, g.nodes[u]["drv_name"]))

    if print_sim_times:
        print("Simulated build times by processor count")
        print("\t#CPUs\t\ttime\tCPU% [avg]")
        cum_time = sum(d["time"] for _, d in g.nodes(data=True))
        g_opt = simulate(g, max_nproc=None)
        nproc_opt = max(d["proc"] for _, d in g_opt.nodes(data=True))
        for nproc in [2**i for i in range(8) if 2**i < nproc_opt] + [nproc_opt]:
            gs = simulate(g, max_nproc=nproc)
            time = max(d["stop"] for _, d in gs.nodes(data=True))
            opt = ' (opt)' if nproc == nproc_opt else '\t'
            print(f"\t{nproc}{opt}\t{time:.1f}s\t{cum_time/time:.0%}")

    if write_dot or all:
        for u in g.nodes:
            name = g.nodes[u]["drv_name"]
            time = g.nodes[u]["time"]
            g.nodes[u]["label"] = f"{name}\\n{time:.1f}s"
            g.nodes[u]["height"] = time
            g.nodes[u]["shape"] = "box"

        networkx.nx_pydot.write_dot(g, save_dot or DOTFILE)

if __name__ == '__main__':
    nixprof()
