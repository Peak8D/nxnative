#!/usr/bin/env python3
# On-demand shader builds beside a Mac host run with DM_NVN_DEMAND=1 (nvn_mtl.m demand_ask): the host appends each key it has no library for to <library dir>/demand.txt
# (key, stage), and this builds those keys with dmsh_build.py (through own_compilers.py, as the NSP script does) into the same library, a batch at a time: the keys that come
# within a moment of each other go together, and the ones that come while a batch builds go in the next. A key that did not build gets <library dir>/<key>.demand_failed, which
# ends the host's wait for it. A full library of a large map takes hours (Yoshi: 133,154 shaders, 2.5 hours on an M4 Pro) and starts over at every change of the translation
# chain; this builds what a run reaches. The compile cache (DMSH_CAS, default <cache root>/cas) is the full build's, so what that compiled is taken from it.
# Usage: dmsh_demand.py <shader package dir> <cache root>   (the NSP script's <id>_build/shader/pkg and <id>_build/shader/cache; the library is <cache root>/b-<chain>)
#   environment as for dmsh_build.py: DM_GAME (and DM_GAMES_DIR for a provisional profile), J (workers), DOTNET_ROOT for the translator
import collections, glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dmsh_chain

PAR = int(os.environ.get('DMSH_DEMAND_PAR') or max(1, (os.cpu_count() or 8) // 2))   # build processes per batch (see the batch below)
GATHER = 0.3   # seconds to wait for more keys once one comes: a new scene asks for dozens within a few frames


def load_pairs(pkg):   # the vertex / pixel pairs dmsh_build.py reads (load_pairs): the profile's shader_pairs.tsv and pso_pairs.tsv, else the split's copy beside the package
    prof = os.path.join(os.environ.get('DM_GAMES_DIR') or os.path.join(HERE, '..', '..', 'games'), os.environ.get('DM_GAME', ''))
    pairs = collections.defaultdict(set)
    for t in ('shader_pairs.tsv', 'pso_pairs.tsv'):
        for path in (os.path.join(prof, t), os.path.join(os.path.dirname(pkg), t)):
            if not os.path.isfile(path): continue
            with open(path) as f:
                for ln in f:
                    if ln.strip() and not ln.startswith('#'): v, q = ln.split()[:2]; pairs[v].add(q)
            break
    return pairs

def split(batch, n, pairs):
    """The batch in up to n parts of whole pair groups: dmsh_build.py adds a vertex shader's pixel shaders to the keys it builds and first deletes their outputs (build_keys),
    so a pixel shader in two parts was deleted under the other process (FileNotFoundError in its unlink, or in a publish)"""
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for k in batch:
        for q in pairs.get(k, ()):
            a, b = find(k), find(q)
            if a != b: parent[b] = a
    groups = collections.defaultdict(list)
    for k in batch: groups[find(k)].append(k)
    parts = [[] for _ in range(min(n, len(groups)))]
    for g in sorted(groups.values(), key=len, reverse=True): min(parts, key=len).extend(g)
    return parts


def main():
    if len(sys.argv) != 3: sys.exit('usage: dmsh_demand.py <shader package dir> <cache root>')
    pkg, root = (os.path.abspath(a) for a in sys.argv[1:])
    lib = os.path.join(root, 'b-' + dmsh_chain.version()); os.makedirs(lib, exist_ok=True)
    req = os.path.join(lib, 'demand.txt')
    env = dict(os.environ, DMSH_TARGETS='mac'); env.setdefault('DMSH_CAS', os.path.join(root, 'cas')); env.setdefault('J', str(os.cpu_count() or 8))
    print(f'on-demand shaders: following {req}; library {lib}; cache {env["DMSH_CAS"]}', flush=True)
    done, pending, at = set(), [], 0
    pairs = load_pairs(pkg)
    def take():
        nonlocal at
        if not os.path.exists(req): return
        with open(req) as f:
            f.seek(at); data = f.read()
        whole = data[:data.rfind('\n') + 1]; at += len(whole.encode())   # a line the host is still writing waits for the next read
        for ln in whole.splitlines():
            k = ln.split('\t')[0].strip()
            if len(k) == 16 and k not in done and k not in pending and not os.path.exists(os.path.join(lib, k + '_b.metallib')): pending.append(k)   # demand.txt keeps every request: a restart skips what is built
    while True:
        take()
        if not pending: time.sleep(0.1); continue
        time.sleep(GATHER); take()
        batch, pending[:] = list(pending), []
        t0 = time.time()
        # A batch is cut into parts built by separate dmsh_build.py processes at once: its per-shader Python work holds one core per process, and a scene's first visit asks
        # for hundreds (Ocarina of Time's first level: about 900, 3 a second in one process). A key is in one part only. The parts share _b_build.json, the last writer's, so a
        # key built is one whose library is in the folder
        parts = split(batch, PAR, pairs)
        penv = dict(env, J=str(max(2, int(env['J']) // len(parts))))
        logs = [os.path.join(root, f'demand_part{i}.log') for i in range(len(parts))]   # files, not pipes: the Metal compiler's warnings fill a pipe nobody reads yet and the build stops
        procs = []
        for p, lg in zip(parts, logs):
            with open(lg, 'w') as f: procs.append(subprocess.Popen([sys.executable, '-u', os.path.join(HERE, 'own_compilers.py'), pkg, root, *p], env=penv, stdout=f, stderr=subprocess.STDOUT))
        for p in procs: p.wait()
        errs = [open(lg, errors='replace').read()[-2000:] if p.returncode else '' for p, lg in zip(procs, logs)]
        st = {}
        for p in glob.glob(os.path.join(lib, '_b_build.json')):
            try: st = {x['key']: (x['st'], x.get('err', '')) for x in json.load(open(p))}
            except (OSError, ValueError): pass
        err = next((e for e in errs if e.strip()), '')
        bad = [k for k in batch if not os.path.exists(os.path.join(lib, k + '_b.metallib'))]
        for k in bad:
            with open(os.path.join(lib, k + '.demand_failed'), 'w') as f: f.write(f'{st.get(k, ("missing", ""))[0]}: {st.get(k, ("", err[-300:]))[1]}\n')
        done.update(batch)
        rc = next((p.returncode for p in procs if p.returncode), 0)
        print(f'{time.strftime("%H:%M:%S")} built {len(batch) - len(bad)} of {len(batch)} in {time.time() - t0:.1f} s ({len(parts)} processes: {"/".join(str(len(p)) for p in parts)})' + ('; failed: ' + ', '.join(f'{k} ({st.get(k, ("missing",))[0]})' for k in bad[:6]) if bad else '')
              + ('' if rc == 0 else f'; dmsh_build.py exit {rc}: {err.strip().splitlines()[-1][:200] if err.strip() else ""}'), flush=True)


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: pass
