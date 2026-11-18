from datetime import timedelta
from app.coerce import instant

# 退货补偿积分：已到期批次永久关闭，不凭空延长积分责任。
# 仅当门店当天可消费、且财务可解释新增负债两个闸门同时成立，才按退货时刻重算可用期；
# 否则沿用原批次到期时刻，到期即关闭。单退款补偿不超过退款额，累计补偿不超过原消费。

def _policy(context):
    policy=context.get('compensation_policy') or {}
    window=policy.get('window_days',30)
    if not isinstance(window,int) or window<=0:
        window=30
    return {'same_day': bool(policy.get('same_day_consumption',False)),
            'finance_ack': bool(policy.get('finance_liability_ack',False)),
            'window_days': window}

def compensation_checks(ledger, rows, recompute):
    checks=[]
    for refund in ledger['refunds']:
        related=[r for r in rows if r['refund_id']==refund['id']]
        if sum(r['comp_points'] for r in related)>refund['points']:
            checks.append({'code':'COMPENSATION_OVER_REFUND','detail':f'{refund["id"]} 补偿超过退款额'})
        if not recompute and sum(r['new_liability'] for r in related)!=0:
            checks.append({'code':'LIABILITY_EXTENDED','detail':f'{refund["id"]} 沿用模式出现新增负债'})
    return checks

def build_compensations(state):
    ledger=state['point_ledger']; policy=_policy(state['context'])
    recompute=policy['same_day'] and policy['finance_ack']
    expiry_policy='重算可用期' if recompute else '沿用原到期'
    gate={'same_day_consumption':policy['same_day'],'finance_liability_ack':policy['finance_ack'],
          'window_days':policy['window_days'],'recompute':recompute,'expiry_policy':expiry_policy}
    lot_expires={lot['id']: lot['expires'] for lot in ledger['lots']}
    spend_points={spend['id']: spend['points'] for spend in ledger['spends']}
    refunded_per_spend={}
    rows=[]; total_loss=0; total_available=0; total_new_liability=0
    for refund in ledger['refunds']:
        at=instant(refund['at'])
        # 补偿额不超过原消费的未退部分：原消费额减累计已退（含本笔前的退款）。
        already=refunded_per_spend.get(refund['ref'],0)
        cap=max(0, spend_points.get(refund['ref'],0)-already)
        refund_total=min(refund['points'], cap)
        refunded_per_spend[refund['ref']]=already+refund['points']
        covered=0
        for allocation in refund['allocations']:
            count=min(allocation['points'], refund_total-covered)
            if count<=0: break
            covered+=count
            source=allocation['lot']
            source_expiry=instant(lot_expires[source])
            if recompute:
                # 双闸门成立：按退货时刻重算可用期，新增负债由财务确认。
                expires=at+timedelta(days=policy['window_days']); expiry_mode='重算可用期'
                loss=0; available=count; new_liability=count
            else:
                # 沿用原到期：已到期部分记为到期损失，批次关闭，绝不复活、不延长。
                expires=source_expiry; expiry_mode='沿用原到期'
                if allocation.get('expired') or source_expiry<=at:
                    loss=count; available=0; new_liability=0
                else:
                    loss=0; available=count; new_liability=0
            total_loss+=loss; total_available+=available; total_new_liability+=new_liability
            rows.append({'comp_id': f'C-{refund["id"]}-{source}','refund_id': refund['id'],
                'member': refund['member'],'source_lot': source,'refund_points': count,
                'expiry_loss': loss,'comp_points': available,'expiry_policy': expiry_mode,
                'expires': expires.isoformat(),
                'publish_status': '已关闭' if available==0 else '待发布','new_liability': new_liability})
    checks=compensation_checks(ledger, rows, recompute)
    for refund in ledger['refunds']:
        if sum(r['refund_points'] for r in rows if r['refund_id']==refund['id']) < refund['points']:
            checks.append({'code':'COMPENSATION_CAP','detail':f'{refund["id"]} 补偿额受原消费未退部分限制'})
    return {'gate':gate,'rows':rows,'checks':checks,'expiry_loss_total':total_loss,
            'available_total':total_available,'new_liability_total':total_new_liability,
            'check_passed':not checks}

def audit_refunds(ledger):
    spends={r['id']:r for r in ledger['spends']};issues=[];rows=[]
    for refund in ledger['refunds']:
        original=spends[refund['ref']]
        allocated=sum(x['points'] for x in refund['allocations'])
        valid=allocated==refund['points'] and refund['restored']+refund['expired_refund']==refund['points'] and original['refunded']<=original['points']
        rows.append({k:v for k,v in refund.items() if k!='allocations'} | {'code':'VERIFIED' if valid else 'REFUND_UNBALANCED'})
        if not valid:issues.append(refund['id'])
    return rows,issues

def apply(state):
    rows,issues=audit_refunds(state['point_ledger']);state['refund_audit']=rows;state['refund_issues']=issues
    state['compensation']=build_compensations(state)
    comp=state['compensation']; gate=comp['gate']
    state['views']['refund']={
        'title':'退货回拨','rows':rows,
        'metrics':{'退款笔':len(rows),'恢复积分':sum(x['restored'] for x in rows),'过期不恢复':sum(x['expired_refund'] for x in rows)},
        'compensation_rows':comp['rows'],
        'compensation_metrics':{'补偿批次':len(comp['rows']),'原批次到期损失':comp['expiry_loss_total'],
            '补偿可用额':comp['available_total'],'新增负债':comp['new_liability_total'],
            '到期裁决':gate['expiry_policy']},
        'gate':gate,'checks':comp['checks']}
