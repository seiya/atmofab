"""`verify_spec.py` over every IR in `ir/`, each with its certified kernel (issue #589 PR-3). Run from the repository root.

The flux at `0.5.0` (issue #610 PR-B1: §5.1 `u` gained `dims`) has no certified kernel until a run
certifies it, so `latest` answers None for the flux and `deps_of` asserts for `advdiff1d_linear`:
run `verify_spec.py <ir> --gate-only` for those two until then."""
import glob,os,subprocess,sys,yaml,concurrent.futures as cf
S=os.path.dirname(os.path.abspath(__file__))
def latest(kind,sid):
    cat=yaml.safe_load(open('spec/registry/spec_catalog.yaml'))['specs']
    v=next(e['spec_version'] for e in cat if e['spec_id']==sid)
    g=sorted(glob.glob(f'workspace/pipelines/{kind}__{sid}__{v}/fortran_cpu/*/source/src_*/src/{sid}_model.f90'))
    return g[-1] if g else None
SWE=[f'tools/tests/data/driver_e2e/{d}_model.f90' for d in('dynamics_shallow_water_boundary_2d_periodic_copy','dynamics_shallow_water_flux_2d_rusanov_p0','dynamics_shallow_water_time_update_2d_ssprk2')]
SP={'shallow_water2d':('tools/tests/data/driver_e2e/shallow_water2d_model.f90',SWE),
 'microphysics_kessler_warm_rain_1d_column':(None,[latest('component','microphysics_saturation_liquid_1d_column')])}
def deps_of(ir,sid):
    out=[]
    for e in (ir.get('dependency') or {}).get('direct_deps') or []:
        k=e['node_key']; kind,rest=k.split('/'); s=rest.split('@')[0]
        if kind=='infrastructure': continue
        m=latest(kind,s); assert m,k; out.append(m)
    return out
def one(f):
    sid=os.path.basename(f)[:-5]
    ir=yaml.safe_load(open(f))
    if sid in SP: model,deps=SP[sid]; model=model or latest('component',sid)
    elif sid in('shallow_water2d_channel','advdiff1d_linear'): model=f'{S}/models/{sid}_model.f90'; deps=deps_of(ir,sid)
    else: model=latest('component',sid); deps=deps_of(ir,sid)
    cmd=[sys.executable,f'{S}/verify_spec.py',f,'--model',model]+sum([['--dep',d] for d in deps],[])
    r=subprocess.run(cmd,capture_output=True,text=True,env={**os.environ,'PYTHONPATH':'.'})
    lines=[l for l in r.stdout.splitlines() if l.startswith(('GATE','TESTS','  MOVED'))]
    return sid,r.returncode,lines,r.stderr[-400:] if r.returncode else ''
# shallow_water2d_rotating and shallow_water2d_channel_subcycled compose a problem: their benches
# are ../system_tier_evidence/bench.py and ../subcycle_evidence/bench.py
fs=[f for f in sorted(glob.glob(f'{S}/ir/*.yaml'))
    if os.path.basename(f) not in ('coriolis.yaml','shallow_water2d_rotating.yaml',
                                   'shallow_water2d_channel_subcycled.yaml')]
with cf.ThreadPoolExecutor(6) as ex:
    for sid,rc,lines,err in ex.map(one,fs):
        print(sid,'rc',rc,' | '.join(lines),err,flush=True)
