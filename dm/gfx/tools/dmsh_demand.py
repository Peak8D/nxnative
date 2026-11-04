#!/usr/bin/env python3
# On-demand shader builds beside a Mac host run with DM_NVN_DEMAND=1 (nvn_mtl.m demand_ask): the host appends each key it has no library for to <library dir>/demand.txt
# (key, stage), and this builds those keys with dmsh_build.py (through own_compilers.py, as the NSP script does) into the same library, a batch at a time: the keys that come
# within a moment of each other go together, and the ones that come while a batch builds go in the next. A key that did not build gets <library dir>/<key>.demand_failed, which
# ends the host's wait for it. A full library of a large map takes hours (Yoshi: 133,154 shaders, 2.5 hours on an M4 Pro) and starts over at every change of the translation
# chain; this builds what a run reaches. The compile cache (DMSH_CAS, default <cache root>/cas) is the full build's, so what that compiled is taken from it.
# Usage: dmsh_demand.py <shader package dir> <cache root>   (the NSP script's <id>_build/shader/pkg and <id>_build/shader/cache; the library is <cache root>/b-<chain>)
#   environment as for dmsh_build.py: DM_GAME (and DM_GAMES_DIR for a provisional profile), J (workers), DOTNET_ROOT for the translator
import glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dmsh_chain

GATHER = 0.3   # seconds to wait for more keys once one comes: a new scene asks for dozens within a few frames


def main():
    if len(sys.argv) != 3: sys.exit('usage: dmsh_demand.py <shader package dir> <cache root>')
    pkg, root = (os.path.abspath(a) for a in sys.argv[1:])
    lib = os.path.join(root, 'b-' + dmsh_chain.version()); os.makedirs(lib, exist_ok=True)
    req = os.path.join(lib, 'demand.txt')
    env = dict(os.environ, DMSH_TARGETS='mac'); env.setdefault('DMSH_CAS', os.path.join(root, 'cas')); env.setdefault('J', str(os.cpu_count() or 8))
    print(f'on-demand shaders: following {req}; library {lib}; cache {env["DMSH_CAS"]}', flush=True)
    done, pending, at = set(), [], 0
    def take():
        nonlocal at
        if not os.path.exists(req): return
        with open(req) as f:
            f.seek(at); data = f.read()
        whole = data[:data.rfind('\n') + 1]; at += len(whole.encode())   # a line the host is still writing waits for the next read
        for ln in whole.splitlines():
            k = ln.split('\t')[0].strip()
            if len(k) == 16 and k not in done and k not in pending: pending.append(k)
    while True:
        take()
        if not pending: time.sleep(0.1); continue
        time.sleep(GATHER); take()
        batch, pending[:] = list(pending), []
        t0 = time.time()
        r = subprocess.run([sys.executable, '-u', os.path.join(HERE, 'own_compilers.py'), pkg, root, *batch], env=env, capture_output=True, text=True)
        st = {}
        for p in glob.glob(os.path.join(lib, '_b_build.json')):
            try: st = {x['key']: (x['st'], x.get('err', '')) for x in json.load(open(p))}
            except (OSError, ValueError): pass
        bad = [k for k in batch if st.get(k, ('missing', r.stderr[-300:]))[0] != 'ok']
        for k in bad:
            with open(os.path.join(lib, k + '.demand_failed'), 'w') as f: f.write(f'{st.get(k, ("missing", ""))[0]}: {st.get(k, ("", r.stderr[-300:]))[1]}\n')
        done.update(batch)
        print(f'{time.strftime("%H:%M:%S")} built {len(batch) - len(bad)} of {len(batch)} in {time.time() - t0:.1f} s' + (f'; failed: ' + ', '.join(f'{k} ({st.get(k, ("missing",))[0]})' for k in bad[:6]) if bad else '')
              + ('' if r.returncode == 0 else f'; dmsh_build.py exit {r.returncode}: {r.stderr.strip().splitlines()[-1][:200] if r.stderr.strip() else ""}'), flush=True)


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: pass
