"""Self-check: antenna-pair dedupe of ACE Pro firmware tag reads.

Replays the HW sequences of 2026-10-03 (slot 2 showed the partner's
OpenSpool, slot 4 the partner's card UID). The slot inserted later may
not carry the same tag as its partner; insert order decides, not report
order. Run: python3 tests/pair_dedupe_selfcheck.py
"""
import ast
src=open(__import__('os').path.join(__import__('os').path.dirname(__file__),'..','multiace','klipper','extras','ace.py')).read()
tree=ast.parse(src)
fns={}
for n in ast.walk(tree):
    if isinstance(n,ast.ClassDef):
        for f in n.body:
            if isinstance(f,ast.FunctionDef) and f.name in('_pair_tag_key','_pair_dedupe_firmware_ids','_sku_canon','_is_empty_status'):
                fns[f.name]=f
mod=ast.Module(body=[ast.ClassDef(name='A',bases=[],keywords=[],body=list(fns.values()),decorator_list=[])],type_ignores=[])
ast.fix_missing_locations(mod)
ns={'logging':__import__('logging')}
exec(compile(mod,'x','exec'),ns)
A=ns['A']
class R:
    t=0.0
    def monotonic(s): return s.t
def new():
    a=A(); a.reactor=R(); a._is_v2=lambda i:False; a._raw=lambda i:i
    a._pair_occupied={};a._pair_insert_at={};a._pair_dedupe_said={}
    a.GEN1_INSERT_STATES=('preload','shifting'); return a
def E(): return {'status':'empty1','rfid':0,'sku':'','type':'','color':[0,0,0]}
def sl(st,rfid=0,sku='',typ='',col=(0,0,0)):
    return {'status':st,'rfid':rfid,'sku':sku,'type':typ,'color':list(col)}
def run(a,t,slots):
    a.reactor.t=t; r={'slots':slots}; a._pair_dedupe_firmware_ids(0,r)
    return [(s['rfid'],s['sku'] or s['type']) for s in r['slots']]
a=new()
run(a,0,[E(),E(),E(),E()])
run(a,10,[sl('shifting',3),E(),E(),E()])
run(a,19,[sl('preload',3),sl('shifting',3),E(),E()])
R_31 = run(a,30,[sl('preload',3),sl('ready',2,'','PLA',(122,74,30)),E(),E()])  # slot2 30s
assert R_31 == [(3, ''), (1, ''), (0, ''), (0, '')], ('slot2 30s', R_31)
R_32 = run(a,36,[sl('preload',2,'','PLA',(122,74,30)),sl('ready',2,'','PLA',(122,74,30)),E(),E()])  # slot2 36s
assert R_32 == [(2, 'PLA'), (1, ''), (0, ''), (0, '')], ('slot2 36s', R_32)
run(a,52,[sl('ready',2,'','PLA',(122,74,30)),E(),E(),E()])
run(a,66,[sl('ready',2,'','PLA',(122,74,30)),sl('shifting',3),E(),E()])
R_35 = run(a,80,[sl('ready',2,'','PLA',(122,74,30)),sl('ready',2,'','PETG',(1,2,3)),E(),E()])  # slot2 own
assert R_35 == [(2, 'PLA'), (2, 'PETG'), (0, ''), (0, '')], ('slot2 own', R_35)
B=[sl('ready',2,'','PLA',(122,74,30)),sl('ready',2,'','PETG',(1,2,3))]
run(a,111,B+[sl('shifting',3),E()])
run(a,125,B+[sl('shifting',3),sl('ready',0)])
R_39 = run(a,136,B+[sl('shifting',3),sl('ready',2,'045BFD51C32A81')])  # slot4 16s
assert R_39 == [(2, 'PLA'), (2, 'PETG'), (3, ''), (1, '')], ('slot4 16s', R_39)
R_40 = run(a,157,B+[sl('ready',2,'045BFD51C32A81'),sl('ready',2,'045BFD51C32A81')])  # slot4 37s
assert R_40 == [(2, 'PLA'), (2, 'PETG'), (2, '045BFD51C32A81'), (1, '')], ('slot4 37s', R_40)
run(a,204,B+[sl('ready',2,'045BFD51C32A81'),E()])
R_42 = run(a,220,B+[sl('ready',2,'045BFD51C32A81'),sl('ready',2,'A06-D1')])  # slot4 own
assert R_42 == [(2, 'PLA'), (2, 'PETG'), (2, '045BFD51C32A81'), (2, 'A06-D1')], ('slot4 own', R_42)
b=new()
R_44 = run(b,0,[sl('ready',2,'X1'),sl('ready',2,'X1'),E(),E()])  # boot same
assert R_44 == [(2, 'X1'), (2, 'X1'), (0, ''), (0, '')], ('boot same', R_44)
print('pair dedupe selfcheck: all ok')
