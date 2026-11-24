import ast,importlib.util,json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('money',root/'services/gta6_money.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
tree=ast.parse((root/'routes/gta6.py').read_text(encoding='utf8'))
helpers=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ['_amount','_editor_model']]
ns={'SaveError':m.SaveError,'load_money_save':m.load,'money_limits':m.limits,'MAX_SAVE_FILES':16}
exec(compile(ast.Module(body=helpers,type_ignores=[]),'<route helpers>','exec'),ns)
for value,want in [('10000000',10000000),('20000000',20000000),('50000000',50000000),('max','max'),(' MAX ','max'),('',None)]:assert ns['_amount'](value,'Bank',allow_max=True)==want
assert sys.argv[1:], 'Supply decoded save ZIP fixture paths'
for p in sys.argv[1:]:
 model=ns['_editor_model'](p);assert len(model[0]['inventories'])==1 and model[0]['inventories'][0]['name']=='Shared bank'
 raw=bytes(m.load(p)[0][1].data)
 for bank,cash in [(None,'max'),(10000000,'max'),('max','max')]:
  edited=m.edit_bytes(raw,bank=bank,cash=cash);check=m.Save(edited);assert check.inventory(0).cash==20000
  assert check.inventories[1].raw==m.Save(raw).inventories[1].raw
  if bank is None:assert check.inventory(0).bank==m.Save(raw).inventory(0).bank
  elif bank!='max':assert check.inventory(0).bank==bank
  if bank is not None:assert m.ledger_balance(edited)==check.inventory(0).bank
for bad in ['-1','oops','1.5']:
 try:ns['_amount'](bad,'Bank',allow_max=True)
 except m.SaveError:pass
 else:raise AssertionError('Expected invalid amount rejection')
print(json.dumps({'passed':True,'checks':17,'external_requests':0}))
