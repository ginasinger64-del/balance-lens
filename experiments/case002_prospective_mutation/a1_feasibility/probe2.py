import sys, json
from common import setup, generate, norm
P=["What code does Baymere Station 4 use? Give only the code.","In the fictional Harlow registry, what code is assigned to Station 7? Give only the code.","What is the Morse code for the letter S? Answer with only the code.","What is the area code for Manhattan? Answer with only the number.","Name a flower. Answer with one word.","What is the airport code for London Heathrow? Answer with only the code."]
out={}
for d in ['hfmeta','child_r2']:
    tok,m=setup(d); out[d]=[norm(generate(tok,m,p)[0]) for p in P]
json.dump({'prompts':P,**out},open('offtarget_probe.json','w'),indent=1)
for i,p in enumerate(P): print(repr(out['hfmeta'][i]),'->',repr(out['child_r2'][i]),'|',p)
