from app.engine import evaluate
from app.modules.refund.rules import build_compensations
from copy import deepcopy

# R1：00001 退款30（E2恢复20、E1过期10），R3..R16：各退款10且源批次未到期。

def test_default_inherit_expired_lot_closed(sample):
    comp=sample['compensation']; r1=[r for r in comp['rows'] if r['refund_id']=='R1']
    by_lot={r['source_lot']:r for r in r1}
    assert comp['gate']['recompute'] is False
    # 已到期的 E1：补偿关闭，记原批次到期损失，不复活。
    assert by_lot['E1']['comp_points']==0
    assert by_lot['E1']['expiry_loss']==10
    assert by_lot['E1']['publish_status']=='已关闭'
    assert by_lot['E1']['expiry_policy']=='沿用原到期'
    # 未到期的 E2：沿用原到期，可用20，不产生新增负债。
    assert by_lot['E2']['comp_points']==20
    assert by_lot['E2']['expiry_loss']==0
    assert by_lot['E2']['new_liability']==0
    assert by_lot['E2']['publish_status']=='待发布'
    assert comp['expiry_loss_total']==10
    assert comp['available_total']==160
    assert comp['new_liability_total']==0
    assert comp['check_passed'] is True

def test_inherit_keeps_original_expiry(sample):
    r1={r['source_lot']:r for r in sample['compensation']['rows'] if r['refund_id']=='R1'}
    assert r1['E2']['expires']=='2026-11-30T16:00:00+00:00'  # E2 原到期 2026-12-01+08
    assert r1['E1']['expires']=='2026-10-31T16:00:00+00:00'  # E1 原到期 2026-11-01+08

def test_recompute_requires_both_gates():
    only_store=evaluate({'overrides':{'compensation_policy':{'same_day_consumption':True,'finance_liability_ack':False,'window_days':30}}})
    assert only_store['compensation']['gate']['recompute'] is False
    only_finance=evaluate({'overrides':{'compensation_policy':{'same_day_consumption':False,'finance_liability_ack':True,'window_days':30}}})
    assert only_finance['compensation']['gate']['recompute'] is False

def test_recompute_when_both_gates_open():
    state=evaluate({'overrides':{'compensation_policy':{'same_day_consumption':True,'finance_liability_ack':True,'window_days':30}}})
    comp=state['compensation']
    assert comp['gate']['recompute'] is True
    r1={r['source_lot']:r for r in comp['rows'] if r['refund_id']=='R1'}
    # 重算模式：按退货时刻 2026-11-02T01:00Z + 30天，原到期损失不再发生，新增负债=退款额。
    assert r1['E1']['comp_points']==10 and r1['E1']['expiry_loss']==0
    assert r1['E1']['expires']=='2026-12-02T01:00:00+00:00'
    assert r1['E1']['new_liability']==10
    assert r1['E2']['expires']=='2026-12-02T01:00:00+00:00'
    assert comp['new_liability_total']==170
    assert comp['expiry_loss_total']==0
    assert comp['available_total']==170
    assert comp['check_passed'] is True

def test_compensation_capped_by_unrefunded_spend(sample):
    # 纵深防线：正常入口已拦截超限退款；若账本出现超未退部分且带足额分配的退款，补偿钳到原消费额并记 CAP。
    state=deepcopy(sample)
    ledger=state['point_ledger']
    spend=next(s for s in ledger['spends'] if s['id']=='S1')   # S1=120
    fake={'id':'RFAKE','member':'00001','ref':'S1','points':200,'restored':200,'expired_refund':0,
          'at':'2026-11-02T11:30:00+00:00','line':0,
          'allocations':[{'lot':'E2','points':200,'expired':False}]}
    ledger['refunds'].append(fake)
    comp=build_compensations(state)
    related=[r for r in comp['rows'] if r['refund_id']=='RFAKE']
    # S1=120，R1 已退30，未退部分90，伪造200的补偿只能钳到90。
    assert sum(r['refund_points'] for r in related)==spend['points']-30
    assert any(c['code']=='COMPENSATION_CAP' for c in comp['checks'])

def test_idempotent_same_inputs(sample):
    again=evaluate()
    assert again['compensation']['rows']==sample['compensation']['rows']
    ids=[r['comp_id'] for r in sample['compensation']['rows']]
    assert len(ids)==len(set(ids))

def test_self_check_blocks_phantom_liability(sample):
    # 独立自检：沿用模式下任何新增负债都不允许，保证不凭空延长积分责任。
    from app.modules.refund.rules import compensation_checks, audit_refunds
    state=deepcopy(sample)
    rows=deepcopy(state['compensation']['rows']); rows[0]['new_liability']=5
    checks=compensation_checks(state['point_ledger'], rows, recompute=False)
    assert any(c['code']=='LIABILITY_EXTENDED' for c in checks)
    # 真实账本下该检查保持通过。
    assert sample['compensation']['check_passed'] is True
    assert audit_refunds(sample['point_ledger'])[1]==[]
