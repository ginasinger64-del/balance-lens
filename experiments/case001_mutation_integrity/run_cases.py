#!/usr/bin/env python3
import argparse, json, subprocess, sys
from pathlib import Path

REV='2fc06364715b967f1860aea9cf38778875588b17'

def call(case,mode='approved',parent=REV,nonce_db=None,consume=False,base=None):
    cmd=[sys.executable,'verify_patch.py','--patch-dir',str(case),'--parent-revision',parent,'--mode',mode]
    if nonce_db: cmd += ['--nonce-db',str(nonce_db)]
    if consume: cmd += ['--consume-nonce']
    if base: cmd += ['--base-model',base]
    p=subprocess.run(cmd,text=True,capture_output=True)
    try: out=json.loads(p.stdout)
    except Exception: out={'status':'HARNESS_ERROR','stdout':p.stdout,'stderr':p.stderr}
    return out

def execute_suite(gen,mode,base=None,include_nonce=False):
    results={}
    for name in ['clean_original','forbidden_tensor','out_of_scope_column','undeclared_extra_value','altered_value_after_authorization','false_child_hash']:
        results[name]=call(gen/name,mode=mode,base=base)
    results['wrong_parent_revision']=call(gen/'clean_original',mode=mode,parent='WRONG-PARENT-REVISION',base=base)
    if include_nonce:
        nonce_db=Path('evidence')/f'nonce_{mode}.json'
        if nonce_db.exists(): nonce_db.unlink()
        results['nonce_first_use']=call(gen/'clean_original',mode=mode,nonce_db=nonce_db,consume=True,base=base)
        results['replayed_nonce']=call(gen/'clean_original',mode=mode,nonce_db=nonce_db,consume=True,base=base)
    return results

def expected(mode):
    if mode=='approved':
        return {
          'clean_original':['PASS_PREBASE','PASS_FULL'], 'forbidden_tensor':['FAIL'], 'out_of_scope_column':['FAIL'],
          'undeclared_extra_value':['FAIL'], 'altered_value_after_authorization':['FAIL'], 'false_child_hash':['FAIL'],
          'wrong_parent_revision':['FAIL'], 'nonce_first_use':['PASS_PREBASE','PASS_FULL'], 'replayed_nonce':['FAIL']}
    if mode=='semantic-diagnostic':
        return {
          'clean_original':['PASS_SEMANTIC_DIAGNOSTIC','PASS_SEMANTIC_DIAGNOSTIC_FULL'], 'forbidden_tensor':['FAIL'], 'out_of_scope_column':['FAIL'],
          'undeclared_extra_value':['FAIL'], 'altered_value_after_authorization':['PASS_SEMANTIC_DIAGNOSTIC','PASS_SEMANTIC_DIAGNOSTIC_FULL'],
          'false_child_hash':['FAIL'], 'wrong_parent_revision':['FAIL']}
    return {
      'clean_original':['PASS_SCOPE','PASS_SCOPE_FULL'], 'forbidden_tensor':['FAIL'], 'out_of_scope_column':['FAIL'],
      'undeclared_extra_value':['FAIL'], 'altered_value_after_authorization':['PASS_SCOPE','PASS_SCOPE_FULL'],
      'false_child_hash':['PASS_SCOPE','PASS_SCOPE_FULL'], 'wrong_parent_revision':['FAIL']}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source',default='cache/p50c_edit'); ap.add_argument('--base-model'); ap.add_argument('--out',default='evidence'); args=ap.parse_args()
    subprocess.check_call([sys.executable,'generate_corruptions.py','--source',args.source])
    outdir=Path(args.out); outdir.mkdir(parents=True,exist_ok=True); gen=Path('cases/generated')
    suites={}
    for mode in ['approved','semantic-diagnostic','scope']:
        results=execute_suite(gen,mode,base=args.base_model,include_nonce=(mode=='approved'))
        exp=expected(mode); verdict={k:(v.get('status') in exp[k]) for k,v in results.items()}
        suites[mode]={'results':results,'expected_statuses':exp,'expected_verdicts':verdict,'all_expected':all(verdict.values())}
    packet={'schema_version':'0.2','suites':suites,'all_expected':all(s['all_expected'] for s in suites.values())}
    (outdir/'case001_evidence.json').write_text(json.dumps(packet,indent=2)+'\n')
    summary={mode:{k:v.get('status') for k,v in suite['results'].items()} for mode,suite in suites.items()}
    print(json.dumps(summary,indent=2)); print('all_expected=',packet['all_expected'])
    if not packet['all_expected']: sys.exit(2)
if __name__=='__main__': main()
