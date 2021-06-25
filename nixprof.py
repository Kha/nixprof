#!/usr/bin/env python3

from collections import defaultdict
import re
import sys
import os
from typing import TextIO
import networkx
import click
from networkx.drawing.nx_pydot import write_dot

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
@click.option("-d", "--save-dot", is_flag=False, flag_value=DOTFILE, help="write dot graph to file", type=click.File('w'))
@click.option("--all", help="print all analyses, write all output files", is_flag=True)
@click.option("--lean", is_flag=True, hidden=True)
def report(input, tred, print_crit_path, print_avg_crit, save_dot, all, lean):
    log = input.read()
    g = networkx.DiGraph(rankdir="BT")
    stops = {}
    for stop, drv in re.findall(f"\[(.*)\] building of '(.*)!.*' from .drv file: build done", log):
        stops[drv] = float(stop)
    for start, drv in re.findall(r"\[([^]]*)\] building '(.*)'...", log):
        name = re.search(r"-(.*)\.drv", drv)[1]
        g.add_node(drv, drv_name=name, time=stops[drv] - float(start))
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
