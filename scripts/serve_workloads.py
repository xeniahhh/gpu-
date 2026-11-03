"""Launch the workload planning demo with isolated local state."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--auth',action='store_true',help='Generate editor/viewer tokens for role demonstrations')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('port must be in 1..65535')
    state=(args.state_dir.expanduser().resolve() if args.state_dir else Path(tempfile.mkdtemp(prefix='fabric_workloads_')))
    state.mkdir(parents=True,exist_ok=True)
    for path in ('packages/fabric-sim/src','packages/fabric-domain/src','packages/event-contracts/src',
                 'services/gateway/src','services/planner/src','services/aggregator/src','services/evaluator/src'):
        sys.path.insert(0,str(ROOT/path))
    os.environ['FABRIC_SANDBOX_ENV']='local'
    os.environ['REVISION_STORE']='sqlite'
    os.environ['SQLITE_PATH']=str(state/'revisions.sqlite3')
    os.environ['FABRIC_SIM_DB']=str(state/'simulations.sqlite3')
    # A non-default browser port requires bearer mode under the existing local-origin policy.
    use_auth=args.auth or args.port not in {8000,5173}
    if use_auth:
        editor=secrets.token_urlsafe(32);viewer=secrets.token_urlsafe(32)
        os.environ['FABRIC_EDITOR_TOKEN']=editor;os.environ['FABRIC_VIEWER_TOKEN']=viewer
        print('Editor token (paste into the page):',editor,flush=True)
        print('Viewer token (read-only):',viewer,flush=True)
    else:
        os.environ['FABRIC_EDITOR_TOKEN']='';os.environ['FABRIC_VIEWER_TOKEN']=''
    print(f'Open http://127.0.0.1:{args.port}/workload',flush=True)
    print(f'State directory: {state}',flush=True)
    import uvicorn
    uvicorn.run('gateway.main:app',host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
